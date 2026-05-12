"""Tests for ``alice_sm.dispatcher`` — the v0 State Machine dispatcher.

Exercised end-to-end with injectable ``list_issues`` / ``post_comment``
callables in place of the ``gh`` CLI. Covers the v0 contract:

  * empty poll → nothing posted, state unchanged
  * happy path → exactly one comment, issue number added to state
  * dedup on second pass → no duplicate comment
  * trust filter rejects: non-trusted author, missing ``art:*`` label,
    >1 ``sm:*`` label, non-canonical ``sm:*`` label
  * state file caps at 1000 entries — oldest dropped first
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_sm import dispatcher as sm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "sm-dispatcher-state.json"


def _make_issue(
    number: int,
    *,
    author: str = "jcronq",
    sm_labels: tuple[str, ...] = ("sm:selected",),
    art_labels: tuple[str, ...] = ("art:code",),
    other_labels: tuple[str, ...] = (),
    title: str = "Test task",
) -> dict:
    """Build a fake ``gh issue list --json`` payload entry."""
    labels = [{"name": n} for n in (*sm_labels, *art_labels, *other_labels)]
    return {
        "number": number,
        "title": title,
        "labels": labels,
        "author": {"login": author},
        "createdAt": "2026-05-12T10:00:00Z",
    }


class Recorder:
    """Captures comments that would have been posted."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, int, str]] = []

    def __call__(self, repo: str, number: int, body: str) -> None:
        self.posted.append((repo, number, body))


def _frozen_now() -> str:
    return "2026-05-12T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_poll_writes_empty_state(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: [],
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert report.polled == 0
    assert report.posted == 0
    assert recorder.posted == []
    # State file is written (empty), so the next run loads cleanly.
    assert state_path.is_file()
    data = json.loads(state_path.read_text())
    assert data == {"version": sm.STATE_VERSION, "hello_commented": []}


def test_happy_path_posts_one_comment(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    issues = [_make_issue(42)]
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert report.posted == 1
    assert report.posted_numbers == [42]
    assert len(recorder.posted) == 1
    repo, number, body = recorder.posted[0]
    assert repo == "jcronq/alice"
    assert number == 42
    # Exact spec-mandated payload.
    assert body == (
        "[SM] dispatcher-hello task=#42 state=sm:selected "
        f"art=art:code ts={_frozen_now()} v=0"
    )
    state = json.loads(state_path.read_text())
    assert state["hello_commented"] == [42]


def test_dedup_skips_already_commented_issues(state_path: pathlib.Path) -> None:
    issues = [_make_issue(42)]
    # First pass: post.
    recorder1 = Recorder()
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder1,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )
    assert len(recorder1.posted) == 1

    # Second pass: same issue still in the slate, must not re-post.
    recorder2 = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder2,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder2.posted == []
    assert report.posted == 0
    assert report.skipped_dedup == 1


def test_untrusted_author_is_skipped(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    issues = [_make_issue(7, author="random-drive-by")]
    logged: list[str] = []
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=logged.append,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert report.skipped_trust == 1
    # State file persists but doesn't contain #7 (we don't mark
    # untrusted issues as "seen" — they're not in our lane).
    state = json.loads(state_path.read_text())
    assert state["hello_commented"] == []
    assert any("untrusted author" in m for m in logged)


def test_missing_art_label_is_skipped(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    issues = [_make_issue(8, art_labels=())]
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert report.skipped_trust == 1


def test_two_sm_labels_is_skipped(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    issues = [_make_issue(9, sm_labels=("sm:selected", "sm:building"))]
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert report.skipped_trust == 1


def test_non_canonical_sm_label_is_skipped(state_path: pathlib.Path) -> None:
    """A typo / unknown ``sm:*`` value must NOT fuzzy-match into the whitelist."""
    recorder = Recorder()
    issues = [_make_issue(10, sm_labels=("sm:building-pleaserun",))]
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert report.skipped_trust == 1


def test_state_caps_at_1000_oldest_evicted(state_path: pathlib.Path) -> None:
    """After 1001 unique issues are recorded, the oldest one drops off."""
    state = sm.DispatcherState()
    # Pre-seed with 1000 issues numbered 1..1000.
    for n in range(1, sm.SEEN_ISSUE_CAP + 1):
        state.mark_hello(n)
    assert len(state.hello_commented) == sm.SEEN_ISSUE_CAP
    # Add one more — the oldest (#1) should fall off.
    state.mark_hello(1001)
    assert len(state.hello_commented) == sm.SEEN_ISSUE_CAP
    assert state.hello_commented[0] == 2
    assert state.hello_commented[-1] == 1001
    assert 1 not in state.hello_commented

    # Also verify save_state enforces the cap if state somehow grows.
    state.hello_commented = list(range(1, sm.SEEN_ISSUE_CAP + 50))  # 1049 entries
    sm.save_state(state_path, state)
    on_disk = json.loads(state_path.read_text())["hello_commented"]
    assert len(on_disk) == sm.SEEN_ISSUE_CAP
    # Oldest dropped: the surviving slice ends at the newest entry.
    assert on_disk[-1] == sm.SEEN_ISSUE_CAP + 49


def test_dry_run_does_not_post_or_persist(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    issues = [_make_issue(11)]
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        dry_run=True,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert report.posted == 1  # in dry-run report.posted reflects intent
    assert report.posted_numbers == [11]
    # Dry-run intentionally does not write the state file.
    assert not state_path.is_file()


def test_gh_failure_returns_nonzero_and_does_not_write_state(
    state_path: pathlib.Path,
) -> None:
    def bad_list(_repo: str) -> list[dict]:
        raise sm.GHCommandError(
            returncode=1,
            stderr="HTTP 401: Bad credentials",
            args=["gh", "issue", "list"],
        )

    recorder = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=bad_list,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 1
    assert recorder.posted == []
    assert report.posted == 0
    # State file must NOT be created on a hard failure — the supervisor
    # retries on the next cadence.
    assert not state_path.is_file()


def test_multiple_mixed_issues_in_one_pass(state_path: pathlib.Path) -> None:
    """Trust-rejected and trust-accepted issues coexist in one slate."""
    recorder = Recorder()
    issues = [
        _make_issue(20),  # valid → post
        _make_issue(21, author="hacker"),  # untrusted → skip
        _make_issue(22, art_labels=()),  # missing art → skip
        _make_issue(23, sm_labels=("sm:selected", "sm:building")),  # 2 sm → skip
        _make_issue(24, sm_labels=("sm:bogus",)),  # typo → skip
        _make_issue(25),  # valid → post
    ]
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert sorted(n for _, n, _ in recorder.posted) == [20, 25]
    assert report.posted == 2
    assert report.skipped_trust == 4
    state = json.loads(state_path.read_text())
    assert sorted(state["hello_commented"]) == [20, 25]


# ---------------------------------------------------------------------------
# Phase 1.5 — label transitions on PR + CI events
# ---------------------------------------------------------------------------


class LabelRecorder:
    """Captures gh_edit_labels invocations."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(
        self,
        repo: str,
        number: int,
        *,
        add: tuple = (),
        remove: tuple = (),
    ) -> None:
        self.calls.append(
            {
                "repo": repo,
                "number": number,
                "add": list(add),
                "remove": list(remove),
            }
        )


class CloseRecorder:
    """Captures gh_close_issue invocations."""

    def __init__(self) -> None:
        self.closed: list[tuple[str, int]] = []

    def __call__(self, repo: str, number: int) -> None:
        self.closed.append((repo, number))


def _no_pr(_repo: str, _n: int) -> dict | None:
    return None


def _no_call(*_a, **_kw) -> dict:
    raise AssertionError("should not be called")


def test_t1_selected_with_linked_pr_transitions_to_reviewing(
    state_path: pathlib.Path,
) -> None:
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(50)]

    def find_pr(_repo: str, n: int) -> dict | None:
        if n == 50:
            return {
                "number": 99,
                "url": "https://github.com/jcronq/alice/pull/99",
                "state": "OPEN",
            }
        return None

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    # Two comments: hello + transition.
    bodies = [body for _r, _n, body in recorder.posted]
    assert any("dispatcher-hello task=#50" in b for b in bodies)
    assert any(
        '[SM] transition from=selected to=reviewing reason="PR opened: '
        'https://github.com/jcronq/alice/pull/99"' in b
        for b in bodies
    )
    # Label edit: add reviewing, remove selected.
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["number"] == 50
    assert edit["add"] == ["sm:reviewing"]
    assert edit["remove"] == ["sm:selected"]
    # Issue NOT closed.
    assert close_rec.closed == []
    assert report.transitioned == 1
    assert (50, "sm:selected", "sm:reviewing") in report.transitions


def test_t1_selected_without_linked_pr_stays(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(51)]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    # Hello posted, no transition.
    assert len(recorder.posted) == 1
    assert "dispatcher-hello" in recorder.posted[0][2]
    assert label_rec.calls == []
    assert close_rec.closed == []
    assert report.transitioned == 0


def test_t2_reviewing_merged_green_ci_transitions_to_done_and_closes(
    state_path: pathlib.Path,
) -> None:
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(60, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, n: int) -> dict | None:
        if n == 60:
            return {"number": 110, "url": "https://github.com/jcronq/alice/pull/110"}
        return None

    def merge_status(_repo: str, pr_number: int) -> dict:
        assert pr_number == 110
        return {
            "merged": True,
            "merge_commit_oid": "abc123def456",
            "pr_url": "https://github.com/jcronq/alice/pull/110",
        }

    def ci(_repo: str, sha: str) -> dict:
        assert sha == "abc123def456"
        return {
            "conclusion": "success",
            "run_url": "https://github.com/jcronq/alice/actions/runs/777",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert close_rec.closed == [("jcronq/alice", 60)]
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:done"]
    assert edit["remove"] == ["sm:reviewing"]
    bodies = [body for _r, _n, body in recorder.posted]
    assert any(
        '[SM] transition from=reviewing to=done reason="PR merged: '
        'https://github.com/jcronq/alice/pull/110, CI green on abc123def456"' in b
        for b in bodies
    )
    assert report.transitioned == 1
    assert (60, "sm:reviewing", "sm:done") in report.transitions


def test_t3_reviewing_merged_red_ci_transitions_to_building_no_close(
    state_path: pathlib.Path,
) -> None:
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(61, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, n: int) -> dict | None:
        return {"number": 111, "url": "https://github.com/jcronq/alice/pull/111"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "deadbeef",
            "pr_url": "https://github.com/jcronq/alice/pull/111",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {
            "conclusion": "failure",
            "run_url": "https://github.com/jcronq/alice/actions/runs/888",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    # NOT closed.
    assert close_rec.closed == []
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:building"]
    assert edit["remove"] == ["sm:reviewing"]
    bodies = [body for _r, _n, body in recorder.posted]
    assert any(
        '[SM] transition from=reviewing to=building reason="CI red on merge: '
        'https://github.com/jcronq/alice/actions/runs/888"' in b
        for b in bodies
    )
    assert report.transitioned == 1
    assert (61, "sm:reviewing", "sm:building") in report.transitions


def test_t2_t3_reviewing_merged_pending_ci_stays(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(62, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 112, "url": "https://example/pr/112"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "1234",
            "pr_url": "https://example/pr/112",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {"conclusion": "pending", "run_url": None}

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert label_rec.calls == []
    assert close_rec.closed == []
    assert report.transitioned == 0


def test_t2_t3_reviewing_pr_still_open_stays(state_path: pathlib.Path) -> None:
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(63, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 113, "url": "https://example/pr/113"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {"merged": False, "merge_commit_oid": None, "pr_url": None}

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert label_rec.calls == []
    assert close_rec.closed == []
    assert report.transitioned == 0


def test_phase_1_5_no_action_on_building_label(state_path: pathlib.Path) -> None:
    """Phase 1.5 doesn't act on sm:building, even with a linked PR."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(70, sm_labels=("sm:building",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 114, "url": "https://example/pr/114"}

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert label_rec.calls == []
    assert close_rec.closed == []
    assert report.transitioned == 0


def test_v0_hello_dedup_still_works_alongside_phase_1_5(
    state_path: pathlib.Path,
) -> None:
    """Mixed state: a previously-helloed sm:selected issue with a fresh
    linked PR still transitions, but does NOT re-post the hello."""
    # Pre-seed state file with #80 already helloed.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": sm.STATE_VERSION, "hello_commented": [80]})
    )

    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [
        _make_issue(80),  # already helloed → skip hello, but transition T1
        _make_issue(81),  # fresh → hello + (no PR) stay
    ]

    def find_pr(_repo: str, n: int) -> dict | None:
        if n == 80:
            return {
                "number": 200,
                "url": "https://example/pr/200",
                "state": "OPEN",
            }
        return None

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    # #80: no hello (dedup), but T1 transition comment posted.
    # #81: hello posted, no transition (no PR).
    bodies_by_number: dict[int, list[str]] = {}
    for _r, n, body in recorder.posted:
        bodies_by_number.setdefault(n, []).append(body)
    assert 80 in bodies_by_number
    assert all("dispatcher-hello" not in b for b in bodies_by_number[80])
    assert any(
        "transition from=selected to=reviewing" in b for b in bodies_by_number[80]
    )
    assert 81 in bodies_by_number
    assert any("dispatcher-hello task=#81" in b for b in bodies_by_number[81])
    # Skipped dedup count for #80.
    assert report.skipped_dedup == 1
    # T1 fired exactly once (for #80).
    assert report.transitioned == 1
    assert (80, "sm:selected", "sm:reviewing") in report.transitions
    # State file: #81 added; #80 retained.
    state = json.loads(state_path.read_text())
    assert sorted(state["hello_commented"]) == [80, 81]


def test_reviewing_merged_pr_with_green_ci_transitions_to_done_via_real_finder(
    state_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end on the real gh_find_linked_pr: a MERGED PR linked to an
    sm:reviewing issue must be discoverable so T2 can fire.

    This test would have failed before the ``--state all`` fix:
    ``gh_find_linked_pr`` previously queried ``--state open``, so a
    merged PR was invisible, _process_reviewing logged
    "no linked PR found — staying", and the issue was stuck at
    sm:reviewing forever (the live #86/#85 repro on jcronq/alice).
    """
    captured_args: list[list[str]] = []

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        captured_args.append(args)
        # gh pr list — return a merged PR linking the issue.
        if "pr" in args and "list" in args:
            return json.dumps(
                [
                    {
                        "number": 85,
                        "url": "https://github.com/jcronq/alice/pull/85",
                        "state": "MERGED",
                        "closingIssuesReferences": [{"number": 86}],
                    }
                ]
            )
        # gh issue list — used by the Phase 1.6 sweep pass to find
        # closed-with-stale-sm:* issues. Not the focus of this test;
        # return an empty list so the sweep is a no-op.
        if "issue" in args and "list" in args:
            return json.dumps([])
        raise AssertionError(f"unexpected gh invocation: {args!r}")

    monkeypatch.setattr(sm, "_run_gh", fake_run_gh)

    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(86, sm_labels=("sm:reviewing",))]

    def merge_status(_repo: str, pr_number: int) -> dict:
        assert pr_number == 85
        return {
            "merged": True,
            "merge_commit_oid": "e434e93",
            "pr_url": "https://github.com/jcronq/alice/pull/85",
        }

    def ci(_repo: str, sha: str) -> dict:
        assert sha == "e434e93"
        return {
            "conclusion": "success",
            "run_url": "https://github.com/jcronq/alice/actions/runs/999",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        # Use the real gh_find_linked_pr — _run_gh is monkeypatched.
        find_linked_pr=sm.gh_find_linked_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _msg: None,
    )

    assert exit_code == 0
    # The fix: gh_find_linked_pr must use --state all.
    pr_list_args = captured_args[0]
    state_idx = pr_list_args.index("--state")
    assert pr_list_args[state_idx + 1] == "all"
    # End-to-end: issue transitioned to sm:done and closed.
    assert close_rec.closed == [("jcronq/alice", 86)]
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:done"]
    assert edit["remove"] == ["sm:reviewing"]
    assert report.transitioned == 1
    assert (86, "sm:reviewing", "sm:done") in report.transitions


# ---------------------------------------------------------------------------
# Phase 1.6 — sweep stale closed issues with non-terminal sm:* labels
# ---------------------------------------------------------------------------


def test_sweep_closed_selected_merged_green_ci_to_done(
    state_path: pathlib.Path,
) -> None:
    """Closed issue stuck at sm:selected with a merged PR + green CI →
    sm:done. The issue stays closed (no re-open, no re-close).
    """
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    stale = [_make_issue(86, sm_labels=("sm:selected",))]

    def find_pr(_repo: str, n: int) -> dict | None:
        assert n == 86
        return {
            "number": 85,
            "url": "https://github.com/jcronq/alice/pull/85",
            "state": "MERGED",
        }

    def merge_status(_repo: str, pr_number: int) -> dict:
        assert pr_number == 85
        return {
            "merged": True,
            "merge_commit_oid": "e434e93cafef00d",
            "pr_url": "https://github.com/jcronq/alice/pull/85",
        }

    def ci(_repo: str, sha: str) -> dict:
        assert sha == "e434e93cafef00d"
        return {
            "conclusion": "success",
            "run_url": "https://github.com/jcronq/alice/actions/runs/999",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: stale,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    # Label flipped: selected → done.
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["number"] == 86
    assert edit["add"] == ["sm:done"]
    assert edit["remove"] == ["sm:selected"]
    # Issue stays closed — never call close_issue from the sweep.
    assert close_rec.closed == []
    # Audit comment posted.
    bodies = [b for _r, _n, b in recorder.posted]
    assert any("[SM] transition from=selected to=done" in b for b in bodies)
    assert any("closed-by-merge sweep" in b for b in bodies)
    assert any("PR #85 merged at e434e93cafef00d" in b for b in bodies)
    # Counters.
    assert report.swept == 1
    assert report.transitioned == 0  # sweep counted separately
    assert (86, "sm:selected", "sm:done") in report.transitions


def test_sweep_closed_reviewing_merged_green_ci_to_done(
    state_path: pathlib.Path,
) -> None:
    """Same path, different starting label (sm:reviewing → sm:done).

    Exercises the helper's non-terminal-label scoping: reviewing is
    just as eligible for sweep as selected when the issue is closed.
    """
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    stale = [_make_issue(88, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {
            "number": 87,
            "url": "https://github.com/jcronq/alice/pull/87",
            "state": "MERGED",
        }

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "feedface",
            "pr_url": "https://github.com/jcronq/alice/pull/87",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {"conclusion": "success", "run_url": "https://example/ci"}

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: stale,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:done"]
    assert edit["remove"] == ["sm:reviewing"]
    assert close_rec.closed == []
    assert report.swept == 1
    assert (88, "sm:reviewing", "sm:done") in report.transitions


def test_sweep_closed_selected_pr_closed_unmerged_to_rejected(
    state_path: pathlib.Path,
) -> None:
    """Closed issue + sm:selected + linked PR closed without merge → sm:rejected."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    stale = [_make_issue(91, sm_labels=("sm:selected",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {
            "number": 90,
            "url": "https://github.com/jcronq/alice/pull/90",
            "state": "CLOSED",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: stale,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        # merge_status / master_ci_status must not be touched when the
        # PR is closed-unmerged; assert that with _no_call.
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:rejected"]
    assert edit["remove"] == ["sm:selected"]
    assert close_rec.closed == []
    bodies = [b for _r, _n, b in recorder.posted]
    assert any("[SM] transition from=selected to=rejected" in b for b in bodies)
    assert any("PR #90 closed without merge" in b for b in bodies)
    assert report.swept == 1


def test_sweep_closed_selected_no_linked_pr_to_rejected(
    state_path: pathlib.Path,
) -> None:
    """Closed issue + sm:selected + no linked PR → sm:rejected (manual close)."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    stale = [_make_issue(92, sm_labels=("sm:selected",))]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: stale,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:rejected"]
    assert edit["remove"] == ["sm:selected"]
    assert close_rec.closed == []
    bodies = [b for _r, _n, b in recorder.posted]
    assert any(
        "issue closed without linked PR (manual close or supersession)" in b
        for b in bodies
    )
    assert report.swept == 1


def test_sweep_closed_selected_merged_red_ci_to_rejected(
    state_path: pathlib.Path,
) -> None:
    """Merged PR with red master CI on a closed sm:selected issue is rejected.

    Rationale: the work shipped but broke master. The merge artifact
    exists, but downstream tracking should treat it as needing
    follow-up — we don't have the Phase 2 quality-gate plumbing yet,
    so the safest terminal is rejected rather than done.
    """
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    stale = [_make_issue(93, sm_labels=("sm:selected",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {
            "number": 100,
            "url": "https://github.com/jcronq/alice/pull/100",
            "state": "MERGED",
        }

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "badbadbad",
            "pr_url": "https://github.com/jcronq/alice/pull/100",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {
            "conclusion": "failure",
            "run_url": "https://github.com/jcronq/alice/actions/runs/777",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: stale,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:rejected"]
    assert edit["remove"] == ["sm:selected"]
    assert close_rec.closed == []
    bodies = [b for _r, _n, b in recorder.posted]
    assert any("[SM] transition from=selected to=rejected" in b for b in bodies)
    assert any("master CI failure" in b for b in bodies)
    assert report.swept == 1


def test_sweep_helper_filters_terminal_labels_defensively() -> None:
    """Even if a closed issue already at sm:done leaks into the listing
    payload, the client-side filter in gh_list_stale_closed_sm_issues
    must drop it. This is a defense-in-depth check — the GitHub search
    qualifier should never return a terminal-labeled issue, but if it
    does we never process it.
    """
    payload = [
        # Terminal — must be dropped.
        {
            "number": 86,
            "title": "already done",
            "labels": [{"name": "sm:done"}, {"name": "art:code"}],
            "author": {"login": "jcronq"},
            "createdAt": "2026-05-12T10:00:00Z",
        },
        # Terminal — must be dropped.
        {
            "number": 87,
            "title": "already rejected",
            "labels": [{"name": "sm:rejected"}, {"name": "art:code"}],
            "author": {"login": "jcronq"},
            "createdAt": "2026-05-12T10:00:00Z",
        },
        # Non-terminal — must be kept.
        {
            "number": 91,
            "title": "stuck",
            "labels": [{"name": "sm:selected"}, {"name": "art:code"}],
            "author": {"login": "jcronq"},
            "createdAt": "2026-05-12T10:00:00Z",
        },
    ]

    captured: list[list[str]] = []

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        captured.append(args)
        return json.dumps(payload)

    import unittest.mock as _mock

    with _mock.patch.object(sm, "_run_gh", fake_run_gh):
        result = sm.gh_list_stale_closed_sm_issues("jcronq/alice")

    # Only the non-terminal issue survives.
    assert [i["number"] for i in result] == [91]
    # Verify the query was scoped to --state closed with the correct
    # non-terminal label set.
    args = captured[0]
    assert "--state" in args and args[args.index("--state") + 1] == "closed"
    search_idx = args.index("--search")
    search_str = args[search_idx + 1]
    assert search_str.startswith("label:")
    # The label list must be exactly the non-terminal set, comma-joined,
    # sorted (deterministic for testability).
    expected_terms = ",".join(sorted(sm.NON_TERMINAL_SM_LABELS))
    assert search_str == f"label:{expected_terms}"
    # And the terminals must NOT appear.
    assert "sm:done" not in search_str
    assert "sm:rejected" not in search_str


def test_sweep_done_line_includes_swept_counter(state_path: pathlib.Path) -> None:
    """The done log line must include ``swept=N`` — Phase 1.6 contract."""
    logged: list[str] = []
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: [],
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=logged.append,
    )
    done_line = [line for line in logged if line.startswith("[sm-dispatcher] done")]
    assert done_line, f"expected a done line in: {logged!r}"
    assert "swept=0" in done_line[0]


# ---------------------------------------------------------------------------
# Phase 2 — auto-spawn claude agents on sm:selected
# ---------------------------------------------------------------------------


class FakePopen:
    """Minimal stand-in for ``subprocess.Popen``.

    Records what it was called with and exposes a fake ``pid``. The
    dispatcher only needs ``proc.pid`` post-spawn; the fake never
    actually runs anything.
    """

    next_pid = 90000

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = FakePopen.next_pid
        FakePopen.next_pid += 1
        # The dispatcher closes its end of stdin/stdout/stderr after
        # construction. We don't need to model that.


def test_phase2_spawn_fires_on_code_artifact_with_no_prior_spawn(
    state_path,
    tmp_path,
) -> None:
    """sm:selected + art:code + no [SM] spawn-started → fires Popen,
    posts spawn-started, writes pidfile and prompt.txt.
    """
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            claude_bin="/usr/bin/claude",
            post_comment=recorder,
            popen=popen,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [_make_issue(200, sm_labels=("sm:selected",), art_labels=("art:code",))]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        list_comments=lambda _r, _n: [],  # no prior spawn-started
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 1
    assert popens, "expected Popen to be called"
    # Popen launched claude with --print.
    cmd = popens[0].args
    assert cmd[-1] == "--print"
    assert cmd[0].endswith("claude")
    # Detached from controlling terminal.
    assert popens[0].kwargs.get("start_new_session") is True
    # Two comments on #200: dispatcher-hello + [SM] spawn-started.
    bodies = [b for _r, n, b in recorder.posted if n == 200]
    assert any(b.startswith("[SM] dispatcher-hello") for b in bodies)
    assert any(b.startswith("[SM] spawn-started") for b in bodies)
    started = [b for b in bodies if b.startswith("[SM] spawn-started")][0]
    assert "task=#200" in started
    assert "artifact=art:code" in started
    assert "runtime=claude-cli" in started
    # spawn dir contains prompt.txt + pidfile.
    spawn_subdirs = [
        d for d in spawn_dir.iterdir() if d.is_dir() and d.name.startswith("spawn-")
    ]
    assert len(spawn_subdirs) == 1
    assert (spawn_subdirs[0] / "prompt.txt").is_file()
    assert (spawn_subdirs[0] / "pidfile").is_file()
    prompt = (spawn_subdirs[0] / "prompt.txt").read_text()
    # Code-worker framing.
    assert "code-worker" in prompt
    assert "open a pr" in prompt.lower()
    assert "Closes #200" in prompt
    assert "Do not --no-verify" in prompt


def test_phase2_spawn_fires_on_research_note_with_writer_template(
    state_path,
    tmp_path,
) -> None:
    """sm:selected + art:research_note → spawn uses research-writer role."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            claude_bin="/usr/bin/claude",
            post_comment=recorder,
            popen=popen,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [
        _make_issue(
            201, sm_labels=("sm:selected",), art_labels=("art:research_note",)
        )
    ]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        list_comments=lambda _r, _n: [],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 1
    assert len(popens) == 1
    spawn_subdirs = [
        d for d in spawn_dir.iterdir() if d.is_dir() and d.name.startswith("spawn-")
    ]
    prompt = (spawn_subdirs[0] / "prompt.txt").read_text()
    assert "research-writer" in prompt
    assert "research note at" in prompt
    assert "sm:selected → sm:done" in prompt
    started = [
        b for _r, n, b in recorder.posted if n == 201 and "spawn-started" in b
    ][0]
    assert "artifact=art:research_note" in started


def test_phase2_already_spawned_marker_skips_new_spawn(
    state_path,
    tmp_path,
) -> None:
    """Issue with a prior [SM] spawn-started from jcronq → no second spawn."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            popen=popen,
            post_comment=recorder,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [_make_issue(202, sm_labels=("sm:selected",), art_labels=("art:code",))]
    existing = [
        {
            "body": (
                "[SM] spawn-started task=#202 artifact=art:code "
                "runtime=claude-cli spawn_id=spawn-202-1 ts=2026-05-12T11:00:00+00:00"
            ),
            "author": {"login": "jcronq"},
        }
    ]

    # Pre-seed dedup so the hello doesn't post either; we're testing
    # spawn dedup, not hello dedup.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": sm.STATE_VERSION, "hello_commented": [202]})
    )

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        list_comments=lambda _r, _n: existing,
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 0
    assert popens == []
    # No new [SM] spawn-started comment posted.
    new_starts = [b for _r, _n, b in recorder.posted if "spawn-started" in b]
    assert new_starts == []


def test_phase2_concurrency_cap_queues_excess_spawns(
    state_path,
    tmp_path,
) -> None:
    """3 sm:selected issues + MAX=2 → 2 spawned, 3rd skipped."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []

    running_count = {"n": 0}

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        # Each new spawn bumps the apparent running count.
        running_count["n"] += 1
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            popen=popen,
            post_comment=recorder,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [
        _make_issue(
            301 + i, sm_labels=("sm:selected",), art_labels=("art:code",)
        )
        for i in range(3)
    ]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        list_comments=lambda _r, _n: [],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: running_count["n"],
        spawn=spawn,
        max_concurrent_spawns=2,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 2
    assert len(popens) == 2
    spawned_nums = [n for n, _a, _id in report.spawn_records]
    # First two in iteration order.
    assert spawned_nums == [301, 302]


def test_phase2_untrusted_author_skipped_before_spawn(
    state_path,
    tmp_path,
) -> None:
    """Trust filter applies before spawn — untrusted author → no spawn."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            popen=popen,
            post_comment=recorder,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [
        _make_issue(
            401,
            sm_labels=("sm:selected",),
            art_labels=("art:code",),
            author="random-drive-by",
        )
    ]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        list_comments=lambda _r, _n: [],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 0
    assert popens == []
    # Trust rejection counted exactly once (not double-counted in
    # spawn pass).
    assert report.skipped_trust == 1


def test_phase2_unrecognized_artifact_label_skips_spawn(
    state_path,
    tmp_path,
) -> None:
    """art:* passes trust but isn't in SPAWN_MAP → no spawn, log line.

    Strategy: shrink SPAWN_MAP via monkeypatch so a trusted ``art:code``
    issue passes the trust filter (which uses the unchanged
    ART_LABEL_WHITELIST) but hits the "no spawn config" branch inside
    :func:`_process_selected`.
    """
    import unittest.mock as mock

    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []
    logged: list[str] = []

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            popen=popen,
            post_comment=recorder,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [
        _make_issue(501, sm_labels=("sm:selected",), art_labels=("art:code",))
    ]

    # SPAWN_MAP without art:code → trust passes (art:code is in the
    # whitelist) but spawn lookup misses.
    with mock.patch.object(sm, "SPAWN_MAP", {}):
        exit_code, report = sm.run(
            repo="jcronq/alice",
            state_path=state_path,
            list_issues=lambda _r: issues,
            list_stale_closed=lambda _r: [],
            list_comments=lambda _r, _n: [],
            post_comment=recorder,
            edit_labels=LabelRecorder(),
            close_issue=CloseRecorder(),
            find_linked_pr=_no_pr,
            pr_merge_status=_no_call,
            master_ci_status=_no_call,
            count_running=lambda: 0,
            spawn=spawn,
            now_iso=_frozen_now,
            log=logged.append,
        )

    assert exit_code == 0
    assert report.spawned == 0
    assert popens == []
    assert any(
        "unrecognized artifact 'art:code'" in m for m in logged
    ), f"expected log line, got: {logged!r}"


def test_phase2_dry_run_logs_spawn_intent_without_popen(
    state_path,
    tmp_path,
) -> None:
    """--dry-run path: spawn pass reports intent and prompt preview but
    never calls Popen or posts the [SM] spawn-started comment.
    """
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    recorder = Recorder()
    popens: list[FakePopen] = []
    logged: list[str] = []

    def popen(*args, **kwargs):
        p = FakePopen(*args, **kwargs)
        popens.append(p)
        return p

    def spawn(issue, art_label, repo):
        return sm.spawn_agent(
            issue,
            art_label,
            repo,
            spawn_dir=spawn_dir,
            popen=popen,
            post_comment=recorder,
            now_iso=_frozen_now,
            log=lambda _m: None,
        )

    issues = [_make_issue(601, sm_labels=("sm:selected",), art_labels=("art:code",))]

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        list_comments=lambda _r, _n: [],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        dry_run=True,
        now_iso=_frozen_now,
        log=logged.append,
    )

    assert exit_code == 0
    assert report.spawned == 1
    assert popens == []
    # No [SM] spawn-started posted in dry-run.
    assert not any("spawn-started" in b for _r, _n, b in recorder.posted)
    assert any("DRY-RUN would spawn on #601" in m for m in logged)
    assert any("DRY-RUN prompt preview" in m for m in logged)
    # spawn_records reflects intent.
    assert report.spawn_records == [(601, "art:code", "<dry-run>")]


def test_phase2_done_line_includes_spawned_counter(state_path) -> None:
    logged: list[str] = []
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: [],
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        now_iso=_frozen_now,
        log=logged.append,
    )
    done = [line for line in logged if line.startswith("[sm-dispatcher] done")]
    assert done, logged
    assert "spawned=0" in done[0]


def test_phase2_count_running_spawns_reaps_dead_pidfiles(tmp_path) -> None:
    """count_running_spawns marks dead PIDs and moves their dirs to .finished/."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()

    # Live pidfile — use this very process.
    live = spawn_dir / "spawn-alive"
    live.mkdir()
    (live / "pidfile").write_text(str(__import__("os").getpid()))

    # Dead pidfile — pick a PID extremely unlikely to be live. We use
    # the kernel's max + 1 (effectively impossible).
    dead = spawn_dir / "spawn-dead"
    dead.mkdir()
    (dead / "pidfile").write_text("99999999")

    live_count = sm.count_running_spawns(spawn_dir)
    assert live_count == 1
    # Dead dir got moved into .finished/.
    assert not dead.exists()
    finished = spawn_dir / ".finished" / "spawn-dead"
    assert finished.exists()


def test_phase2_gh_find_unspawned_selected_issues_filters_by_trusted_author(
    state_path,
) -> None:
    """The standalone helper filters open sm:selected issues, returning
    only those WITHOUT a trusted-author [SM] spawn-started comment.
    """
    issues = [
        _make_issue(701, sm_labels=("sm:selected",), art_labels=("art:code",)),
        _make_issue(702, sm_labels=("sm:selected",), art_labels=("art:code",)),
        _make_issue(703, sm_labels=("sm:selected",), art_labels=("art:code",)),
    ]
    # #701: trusted-author spawn-started → already spawned
    # #702: untrusted-author spawn-started → should still count as
    #       unspawned (someone could spoof the prefix)
    # #703: no comments → unspawned
    comment_map = {
        701: [
            {
                "body": "[SM] spawn-started task=#701 artifact=art:code",
                "author": {"login": "jcronq"},
            }
        ],
        702: [
            {
                "body": "[SM] spawn-started task=#702 artifact=art:code",
                "author": {"login": "drive-by"},
            }
        ],
        703: [],
    }

    def list_issues(_r):
        return issues

    def list_comments(_r, n):
        return comment_map[n]

    unspawned = sm.gh_find_unspawned_selected_issues(
        "jcronq/alice",
        list_issues=list_issues,
        list_comments=list_comments,
    )
    assert sorted(i["number"] for i in unspawned) == [702, 703]
