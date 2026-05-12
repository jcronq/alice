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
            return {"number": 99, "url": "https://github.com/jcronq/alice/pull/99"}
        return None

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
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
            return {"number": 200, "url": "https://example/pr/200"}
        return None

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
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
