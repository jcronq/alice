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
import subprocess

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
    assert data == {
        "version": sm.STATE_VERSION,
        "hello_commented": [],
        "verify_failed_posted": [],
        "needs_study_hinted": [],
    }


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


# Issue #150 — SM v2 pipeline labels are whitelisted but not yet routed.
# The dispatcher's main switch falls through to "no action this phase";
# the trust filter must accept them as valid sm:* values.
# Issue #157 adds the ``sm:needs_study`` handler, so that label dropped
# off this "no action" matrix — covered by its own test block below.
V2_PIPELINE_LABELS = (
    "sm:designing",
    "sm:design_review",
    "sm:designed",
    "sm:compacting",
    "sm:building",
)


@pytest.mark.parametrize("sm_label", V2_PIPELINE_LABELS)
def test_v2_pipeline_label_is_in_whitelist(sm_label: str) -> None:
    assert sm_label in sm.SM_LABEL_WHITELIST
    assert sm_label not in sm.TERMINAL_SM_LABELS
    assert sm_label in sm.NON_TERMINAL_SM_LABELS


@pytest.mark.parametrize("sm_label", V2_PIPELINE_LABELS)
def test_v2_pipeline_label_logs_no_action_this_phase(
    state_path: pathlib.Path, sm_label: str
) -> None:
    """An issue at one of the new v2 states lists cleanly and the
    dispatcher logs ``no action this phase`` rather than rejecting it
    as an unknown sm:* label.
    """
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(150, sm_labels=(sm_label,))]
    logged: list[str] = []

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
        log=logged.append,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert label_rec.calls == []
    assert close_rec.closed == []
    assert report.transitioned == 0
    assert report.skipped_trust == 0
    assert any(
        f"#150 at {sm_label} — no action this phase" in m for m in logged
    ), logged
    assert not any("expected exactly one whitelisted sm:* label" in m for m in logged)


@pytest.mark.parametrize("sm_label", V2_PIPELINE_LABELS)
def test_v2_pipeline_label_without_art_still_rejected(
    state_path: pathlib.Path, sm_label: str
) -> None:
    """The trust filter still requires an ``art:*`` label. Whitelisting
    new sm:* states must not relax that gate.

    Note: with the new label-routed main loop the missing-art:* check
    runs only inside ``_process_selected`` (after the sm-label switch).
    For the new v2 states the dispatcher takes the no-op branch, so a
    missing art:* doesn't bump ``skipped_trust`` — but the issue still
    gets no action (no comments, no transitions, no spawns).
    """
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(151, sm_labels=(sm_label,), art_labels=())]

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
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert recorder.posted == []
    assert label_rec.calls == []
    assert close_rec.closed == []
    assert report.transitioned == 0


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
        enable_cleanup=False,
        enable_verify=False,
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
        enable_cleanup=False,
        enable_verify=False,
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
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 1
    assert popens, "expected Popen to be called"
    # Popen launched claude with --print and a pre-minted --session-id
    # (issue #137 — capture worker session for the viewer trace).
    cmd = popens[0].args
    assert cmd[0].endswith("claude")
    assert "--print" in cmd
    assert "--session-id" in cmd
    sid_idx = cmd.index("--session-id")
    assert sid_idx + 1 < len(cmd), "expected a UUID after --session-id"
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
    # spawn dir contains prompt.txt + pidfile + session_id (#137).
    spawn_subdirs = [
        d for d in spawn_dir.iterdir() if d.is_dir() and d.name.startswith("spawn-")
    ]
    assert len(spawn_subdirs) == 1
    assert (spawn_subdirs[0] / "prompt.txt").is_file()
    assert (spawn_subdirs[0] / "pidfile").is_file()
    sid_file = spawn_subdirs[0] / "session_id"
    assert sid_file.is_file()
    # And it matches the value passed to --session-id.
    assert sid_file.read_text().strip() == cmd[sid_idx + 1]
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
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
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


def test_phase2_live_spawn_dir_skips_new_spawn(
    state_path,
    tmp_path,
) -> None:
    """Issue with a live spawn-<n>-* dir → no second spawn.

    Issue #115: the previous contract dedup-ed on the
    ``[SM] spawn-started`` audit comment alone. The new contract is
    that the live spawn dir is ground truth — a comment alone is not
    enough to skip.
    """
    import os as _os

    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    # Pre-create a live spawn dir for #202, pidfile pointing at this
    # very process (guaranteed alive for the duration of the test).
    live_spawn = spawn_dir / "spawn-202-1778600000"
    live_spawn.mkdir()
    (live_spawn / "pidfile").write_text(str(_os.getpid()))

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
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        has_live_spawn=lambda n: sm.has_live_spawn_for_issue(n, spawn_dir),
        count_running=lambda: 1,
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 0
    assert popens == []
    # No new [SM] spawn-started comment posted.
    new_starts = [b for _r, _n, b in recorder.posted if "spawn-started" in b]
    assert new_starts == []
    # Live dir was not reaped.
    assert live_spawn.exists()


def test_phase2_stale_spawn_dir_falls_through_and_respawns(
    state_path,
    tmp_path,
) -> None:
    """Issue with only a dead-pidfile spawn-<n>-* dir → reap + new spawn.

    Issue #115: this is the worker-died-mid-flight recovery case.
    The historic ``[SM] spawn-started`` audit comment is irrelevant —
    the dispatcher checks the spawn dir, finds a dead PID, moves the
    stale dir to ``.finished/``, and proceeds to spawn.
    """
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    # Pre-create a stale spawn dir for #203 with a PID that's
    # effectively guaranteed dead (kernel pid_max + 1).
    stale_spawn = spawn_dir / "spawn-203-1778500000"
    stale_spawn.mkdir()
    (stale_spawn / "pidfile").write_text("99999999")

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

    issues = [_make_issue(203, sm_labels=("sm:selected",), art_labels=("art:code",))]

    # Pre-seed dedup so the hello doesn't post either.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": sm.STATE_VERSION, "hello_commented": [203]})
    )

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda _r: issues,
        list_stale_closed=lambda _r: [],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        has_live_spawn=lambda n: sm.has_live_spawn_for_issue(n, spawn_dir),
        count_running=lambda: 0,
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.spawned == 1
    assert len(popens) == 1
    # Stale dir was reaped into .finished/.
    assert not stale_spawn.exists()
    assert (spawn_dir / ".finished" / stale_spawn.name).exists()
    # A fresh spawn-started comment was posted by the new worker spawn.
    new_starts = [b for _r, _n, b in recorder.posted if "spawn-started" in b]
    assert len(new_starts) == 1
    assert "task=#203" in new_starts[0]


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
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: running_count["n"],
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
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
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
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
            post_comment=recorder,
            edit_labels=LabelRecorder(),
            close_issue=CloseRecorder(),
            find_linked_pr=_no_pr,
            pr_merge_status=_no_call,
            master_ci_status=_no_call,
            count_running=lambda: 0,
            spawn=spawn,
            proactive_reap=lambda: (0, 0),
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
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_no_pr,
        pr_merge_status=_no_call,
        master_ci_status=_no_call,
        count_running=lambda: 0,
        spawn=spawn,
        proactive_reap=lambda: (0, 0),
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


# ---------------------------------------------------------------------------
# Issue #137 — worker session JSONL capture on reap


def test_reap_copies_session_jsonl_into_spawn_dir(tmp_path) -> None:
    """When a spawn dir has a session_id pointing at a JSONL in the
    fake projects dir, _reap_spawn_dir copies it into ``session.jsonl``
    before the rename so the finished spawn dir is self-contained."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    fake_projects = tmp_path / "fake-claude-projects"
    fake_projects.mkdir()

    sid = "11111111-2222-3333-4444-555555555555"
    project_subdir = fake_projects / "-some-cwd"
    project_subdir.mkdir()
    src = project_subdir / f"{sid}.jsonl"
    src.write_text('{"type":"user","message":{"content":"hi"}}\n')

    dead = spawn_dir / "spawn-9-1"
    dead.mkdir()
    (dead / "pidfile").write_text("99999999")  # PID definitely not alive
    (dead / sm.SESSION_ID_FILENAME).write_text(sid)

    finished_root = spawn_dir / ".finished"
    sm._reap_spawn_dir(dead, finished_root, projects_dir=fake_projects)

    moved = finished_root / "spawn-9-1"
    assert moved.is_dir()
    assert (moved / sm.SESSION_JSONL_FILENAME).is_file()
    assert (moved / sm.SESSION_JSONL_FILENAME).read_text().startswith('{"type":"user"')


def test_reap_without_session_id_is_noop(tmp_path) -> None:
    """A spawn dir that pre-dates issue #137 (no session_id file) reaps
    cleanly without trying to copy any JSONL."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    fake_projects = tmp_path / "fake-claude-projects"
    fake_projects.mkdir()

    dead = spawn_dir / "spawn-9-1"
    dead.mkdir()
    (dead / "pidfile").write_text("99999999")

    finished_root = spawn_dir / ".finished"
    sm._reap_spawn_dir(dead, finished_root, projects_dir=fake_projects)

    moved = finished_root / "spawn-9-1"
    assert moved.is_dir()
    assert not (moved / sm.SESSION_JSONL_FILENAME).exists()


def test_reap_session_id_present_but_jsonl_missing_logs_and_continues(
    tmp_path,
) -> None:
    """Session id is set but the JSONL doesn't exist (worker crashed
    before persisting). Reap completes without raising; nothing is
    copied."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    fake_projects = tmp_path / "fake-claude-projects"
    fake_projects.mkdir()

    dead = spawn_dir / "spawn-9-1"
    dead.mkdir()
    (dead / "pidfile").write_text("99999999")
    (dead / sm.SESSION_ID_FILENAME).write_text("nonexistent-session-id")

    finished_root = spawn_dir / ".finished"
    logged: list[str] = []
    sm._reap_spawn_dir(
        dead, finished_root, projects_dir=fake_projects, log=logged.append
    )

    moved = finished_root / "spawn-9-1"
    assert moved.is_dir()
    assert not (moved / sm.SESSION_JSONL_FILENAME).exists()
    assert any("no session JSONL found" in m for m in logged)


def test_phase2_has_live_spawn_for_issue_matches_by_issue_number(
    tmp_path,
) -> None:
    """has_live_spawn_for_issue is scoped to ``spawn-<N>-*`` dirs and
    reaps stale matches into ``.finished/``.

    Issue #115 contract: a live spawn dir for the issue → True
    (dedup). Only-stale matches → False, and the stale dirs are reaped.
    A live dir for an unrelated issue must not satisfy a check for
    this issue.
    """
    import os as _os

    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()

    # #100: live dir (this process) — should dedup.
    live_100 = spawn_dir / "spawn-100-1"
    live_100.mkdir()
    (live_100 / "pidfile").write_text(str(_os.getpid()))

    # #200: stale dir (impossible PID) — should not dedup.
    stale_200 = spawn_dir / "spawn-200-1"
    stale_200.mkdir()
    (stale_200 / "pidfile").write_text("99999999")

    # #300: no spawn dir at all — should not dedup.

    assert sm.has_live_spawn_for_issue(100, spawn_dir) is True
    # #100's live dir is preserved.
    assert live_100.exists()

    assert sm.has_live_spawn_for_issue(200, spawn_dir) is False
    # #200's stale dir was reaped into .finished/.
    assert not stale_200.exists()
    assert (spawn_dir / ".finished" / "spawn-200-1").exists()

    assert sm.has_live_spawn_for_issue(300, spawn_dir) is False

    # Per-issue scoping: the live dir for #100 must not satisfy a
    # liveness check for an unrelated issue number whose digits
    # happen to overlap.
    assert sm.has_live_spawn_for_issue(101, spawn_dir) is False
    assert sm.has_live_spawn_for_issue(1, spawn_dir) is False


# ---------------------------------------------------------------------------
# Issue #142 — proactive reap of stale active/ spawn dirs


def _dead_spawn_dir(spawn_dir: pathlib.Path, name: str) -> pathlib.Path:
    """Create a spawn dir with a pidfile pointing at a definitely-dead PID."""
    d = spawn_dir / name
    d.mkdir()
    # kernel pid_max + 1; effectively impossible to be live.
    (d / "pidfile").write_text("99999999")
    return d


def test_proactive_reap_terminal_issue_reaps_dead_dir(tmp_path) -> None:
    """Dead spawn dir whose issue is at sm:done → moved to .finished/."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    dead = _dead_spawn_dir(spawn_dir, "spawn-135-1778637042")

    def get_issue(number: int) -> dict | None:
        assert number == 135
        return {
            "number": 135,
            "state": "CLOSED",
            "labels": [{"name": "sm:done"}, {"name": "art:code"}],
        }

    logged: list[str] = []
    reaped, stuck = sm.proactive_reap_dead_spawns(
        spawn_dir, get_issue=get_issue, log=logged.append
    )

    assert reaped == 1
    assert stuck == 0
    assert not dead.exists()
    assert (spawn_dir / ".finished" / "spawn-135-1778637042").exists()


def test_proactive_reap_stuck_spawn_stays_in_place(tmp_path) -> None:
    """Dead spawn dir + issue still at sm:selected → not reaped, warning logged."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    dead = _dead_spawn_dir(spawn_dir, "spawn-203-1778500000")

    def get_issue(number: int) -> dict | None:
        return {
            "number": 203,
            "state": "OPEN",
            "labels": [{"name": "sm:selected"}, {"name": "art:code"}],
        }

    logged: list[str] = []
    reaped, stuck = sm.proactive_reap_dead_spawns(
        spawn_dir, get_issue=get_issue, log=logged.append
    )

    assert reaped == 0
    assert stuck == 1
    # Spawn dir is still in active/ for human review.
    assert dead.exists()
    assert not (spawn_dir / ".finished" / dead.name).exists()
    # Warning was logged.
    assert any(
        "WARNING" in m and "#203" in m for m in logged
    ), f"expected WARNING for stuck #203 in: {logged!r}"


def test_proactive_reap_live_spawn_untouched(tmp_path) -> None:
    """Live spawn dir is never touched, regardless of issue state."""
    import os as _os

    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    live = spawn_dir / "spawn-400-1778700000"
    live.mkdir()
    (live / "pidfile").write_text(str(_os.getpid()))

    calls: list[int] = []

    def get_issue(number: int) -> dict | None:
        calls.append(number)
        return {
            "number": number,
            "state": "CLOSED",
            "labels": [{"name": "sm:done"}],
        }

    reaped, stuck = sm.proactive_reap_dead_spawns(
        spawn_dir, get_issue=get_issue, log=lambda _m: None
    )

    assert reaped == 0
    assert stuck == 0
    # Live dir untouched and get_issue never consulted (no reason to ask).
    assert live.exists()
    assert calls == []


def test_proactive_reap_progressed_issue_reaps_dead_dir(tmp_path) -> None:
    """Dead spawn dir + issue at sm:reviewing (PR is open) → reaped.

    The worker did its job — the dead pid is just init having reaped
    the long-since-finished subprocess. No human review needed.
    """
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    dead = _dead_spawn_dir(spawn_dir, "spawn-150-1778640000")

    def get_issue(number: int) -> dict | None:
        return {
            "number": 150,
            "state": "OPEN",
            "labels": [{"name": "sm:reviewing"}, {"name": "art:code"}],
        }

    reaped, stuck = sm.proactive_reap_dead_spawns(
        spawn_dir, get_issue=get_issue, log=lambda _m: None
    )

    assert reaped == 1
    assert stuck == 0
    assert not dead.exists()
    assert (spawn_dir / ".finished" / "spawn-150-1778640000").exists()


def test_proactive_reap_unfetchable_issue_leaves_dir_alone(tmp_path) -> None:
    """If get_issue returns None (gh error / 404), the dir is left in place
    for retry on the next cycle."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    dead = _dead_spawn_dir(spawn_dir, "spawn-999-1778600000")

    logged: list[str] = []
    reaped, stuck = sm.proactive_reap_dead_spawns(
        spawn_dir, get_issue=lambda _n: None, log=logged.append
    )

    assert reaped == 0
    assert stuck == 0
    assert dead.exists()
    assert any("could not fetch" in m for m in logged)


def test_proactive_reap_skips_non_canonical_dir_name(tmp_path) -> None:
    """A subdir that doesn't match ``spawn-<N>-<ts>`` is skipped — defensive
    against random files / future-format dirs."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    bogus = spawn_dir / "not-a-spawn-dir"
    bogus.mkdir()
    (bogus / "pidfile").write_text("99999999")

    calls: list[int] = []

    def get_issue(number: int) -> dict | None:
        calls.append(number)
        return None

    reaped, stuck = sm.proactive_reap_dead_spawns(
        spawn_dir, get_issue=get_issue, log=lambda _m: None
    )

    assert reaped == 0
    assert stuck == 0
    assert bogus.exists()
    # No lookup attempted for an unparseable name.
    assert calls == []


def test_proactive_reap_run_integration_invokes_callable(state_path) -> None:
    """run() invokes the injected proactive_reap once per cycle, before
    issue processing."""
    calls: list[int] = []

    def fake_reap() -> tuple[int, int]:
        calls.append(1)
        return (2, 1)

    recorder = Recorder()
    exit_code, _report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: [],
        post_comment=recorder,
        proactive_reap=fake_reap,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert calls == [1]


def test_proactive_reap_run_integration_swallows_oserror(state_path) -> None:
    """If the proactive reap raises OSError, run() logs and continues —
    a transient filesystem hiccup must not block the main poll."""

    def crashy_reap() -> tuple[int, int]:
        raise OSError("disk gone")

    logged: list[str] = []
    recorder = Recorder()
    exit_code, _report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: [],
        list_stale_closed=lambda _r: [],
        post_comment=recorder,
        proactive_reap=crashy_reap,
        now_iso=_frozen_now,
        log=logged.append,
    )

    assert exit_code == 0
    assert any("proactive-reap failed" in m for m in logged)


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


# ---------------------------------------------------------------------------
# Selection order — issues should come back oldest-first so the
# concurrency cap can't starve older tasks behind a stream of newer
# arrivals.
# ---------------------------------------------------------------------------


def test_sort_oldest_first_orders_by_created_at() -> None:
    payload = [
        {"number": 107, "createdAt": "2026-05-12T11:30:00Z"},
        {"number": 102, "createdAt": "2026-05-12T09:00:00Z"},
        {"number": 105, "createdAt": "2026-05-12T11:00:00Z"},
        {"number": 104, "createdAt": "2026-05-12T10:00:00Z"},
    ]
    ordered = sm._sort_oldest_first(payload)
    assert [i["number"] for i in ordered] == [102, 104, 105, 107]


def test_sort_oldest_first_breaks_tie_by_issue_number() -> None:
    # Same timestamp → fall back to issue number ascending so order is
    # deterministic across passes.
    payload = [
        {"number": 50, "createdAt": "2026-05-12T10:00:00Z"},
        {"number": 30, "createdAt": "2026-05-12T10:00:00Z"},
        {"number": 40, "createdAt": "2026-05-12T10:00:00Z"},
    ]
    ordered = sm._sort_oldest_first(payload)
    assert [i["number"] for i in ordered] == [30, 40, 50]


def test_sort_oldest_first_missing_created_at_sorts_last() -> None:
    # A malformed payload entry without createdAt must NOT jump the
    # queue; it sorts after all timestamped peers so well-formed tasks
    # win the cap.
    payload = [
        {"number": 1, "createdAt": "2026-05-12T10:00:00Z"},
        {"number": 2},
        {"number": 3, "createdAt": "2026-05-12T09:00:00Z"},
    ]
    ordered = sm._sort_oldest_first(payload)
    assert [i["number"] for i in ordered] == [3, 1, 2]


# ---------------------------------------------------------------------------
# Issue #127 — post-merge working-tree cleanup
# ---------------------------------------------------------------------------


class _FakeGit:
    """Record each ``git`` invocation and return scripted CompletedProcess
    results. Each script entry is a (returncode, stdout, stderr) tuple
    keyed by the first positional argument (e.g. ``"status"``,
    ``"checkout"``, ``"pull"``, ``"branch"``, ``"rev-parse"``).
    """

    def __init__(self, script: dict[str, tuple[int, str, str]]) -> None:
        self.script = script
        self.calls: list[tuple[list[str], pathlib.Path]] = []

    def __call__(
        self, args: list[str], cwd: pathlib.Path
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append((list(args), cwd))
        verb = args[0] if args else ""
        rc, stdout, stderr = self.script.get(verb, (0, "", ""))
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=rc, stdout=stdout, stderr=stderr
        )


def test_post_merge_cleanup_happy_path_switches_pulls_deletes(
    tmp_path: pathlib.Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    logged: list[str] = []
    fake_git = _FakeGit(
        {
            "status": (0, "", ""),  # clean tree
            "rev-parse": (0, "feat/foo-127\n", ""),
            "checkout": (0, "Switched to branch 'master'\n", ""),
            "pull": (0, "Already up to date.\n", ""),
            "branch": (0, "Deleted branch feat/foo-127\n", ""),
        }
    )
    sm._post_merge_cleanup(
        repo_path=repo_path,
        branch="feat/foo-127",
        issue_number=127,
        run_git=fake_git,
        log=logged.append,
    )
    verbs = [c[0][0] for c in fake_git.calls]
    assert verbs == ["status", "rev-parse", "checkout", "pull", "branch"]
    assert fake_git.calls[2][0] == ["checkout", "master"]
    assert fake_git.calls[3][0] == ["pull", "--ff-only", "origin", "master"]
    assert fake_git.calls[4][0] == ["branch", "-d", "feat/foo-127"]
    assert any("switched" in m for m in logged)
    assert any("pulled origin/master" in m for m in logged)
    assert any("deleted local branch 'feat/foo-127'" in m for m in logged)
    # Audit-trail prefix on every line.
    assert all(m.startswith("[SM] checkout #127") for m in logged)


def test_post_merge_cleanup_already_on_master_skips_checkout(
    tmp_path: pathlib.Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    logged: list[str] = []
    fake_git = _FakeGit(
        {
            "status": (0, "", ""),
            "rev-parse": (0, "master\n", ""),
            "pull": (0, "", ""),
        }
    )
    sm._post_merge_cleanup(
        repo_path=repo_path,
        branch=None,
        issue_number=200,
        run_git=fake_git,
        log=logged.append,
    )
    verbs = [c[0][0] for c in fake_git.calls]
    # No ``checkout`` (already on master), no ``branch -d`` (branch=None).
    assert verbs == ["status", "rev-parse", "pull"]
    assert any("already on master" in m for m in logged)


def test_post_merge_cleanup_dirty_tree_skips_everything(
    tmp_path: pathlib.Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    logged: list[str] = []
    fake_git = _FakeGit(
        {
            "status": (0, " M src/dispatcher.py\n", ""),
        }
    )
    sm._post_merge_cleanup(
        repo_path=repo_path,
        branch="feat/foo-127",
        issue_number=127,
        run_git=fake_git,
        log=logged.append,
    )
    verbs = [c[0][0] for c in fake_git.calls]
    # ``status`` runs to detect dirtiness; nothing else fires.
    assert verbs == ["status"]
    assert any("uncommitted changes" in m for m in logged)
    assert any("operator should resolve" in m for m in logged)


def test_post_merge_cleanup_local_branch_already_gone_is_logged_not_error(
    tmp_path: pathlib.Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    logged: list[str] = []
    fake_git = _FakeGit(
        {
            "status": (0, "", ""),
            "rev-parse": (0, "master\n", ""),
            "pull": (0, "", ""),
            "branch": (1, "", "error: branch 'feat/foo-127' not found.\n"),
        }
    )
    sm._post_merge_cleanup(
        repo_path=repo_path,
        branch="feat/foo-127",
        issue_number=127,
        run_git=fake_git,
        log=logged.append,
    )
    assert any("already absent" in m for m in logged)
    # No "failed" log line — already-absent is the idempotent case.
    assert not any("failed" in m for m in logged)


def test_post_merge_cleanup_pull_failure_is_logged_but_non_fatal(
    tmp_path: pathlib.Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    logged: list[str] = []
    fake_git = _FakeGit(
        {
            "status": (0, "", ""),
            "rev-parse": (0, "feat/foo-127\n", ""),
            "checkout": (0, "", ""),
            "pull": (1, "", "fatal: Not possible to fast-forward, aborting.\n"),
            "branch": (0, "Deleted branch feat/foo-127\n", ""),
        }
    )
    sm._post_merge_cleanup(
        repo_path=repo_path,
        branch="feat/foo-127",
        issue_number=127,
        run_git=fake_git,
        log=logged.append,
    )
    # Pull failure logged...
    assert any("git pull --ff-only origin master failed" in m for m in logged)
    # ...but the branch delete still ran (pull failure is non-fatal).
    verbs = [c[0][0] for c in fake_git.calls]
    assert "branch" in verbs


def test_post_merge_cleanup_repo_path_missing_skips(
    tmp_path: pathlib.Path,
) -> None:
    missing = tmp_path / "does-not-exist"
    logged: list[str] = []

    def boom(_args: list[str], _cwd: pathlib.Path) -> None:
        raise AssertionError("run_git must not be called when repo_path is missing")

    sm._post_merge_cleanup(
        repo_path=missing,
        branch="feat/foo-127",
        issue_number=127,
        run_git=boom,  # type: ignore[arg-type]
        log=logged.append,
    )
    assert any("repo path missing" in m for m in logged)


def test_reviewing_done_transition_invokes_cleanup_with_branch(
    state_path: pathlib.Path,
) -> None:
    """T2 reviewing→done must call post_merge_cleanup with the PR's head branch."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(127, sm_labels=("sm:reviewing",))]
    cleanup_calls: list[tuple[str | None, int]] = []

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 250, "url": "https://example/pr/250"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "deadbeefcafe",
            "pr_url": "https://example/pr/250",
            "head_ref_name": "feat/sm-checkout-127",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {"conclusion": "success", "run_url": "https://example/ci"}

    def cleanup(branch: str | None, issue_number: int) -> None:
        cleanup_calls.append((branch, issue_number))

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_verify=False,
        list_issues=lambda _r: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        post_merge_cleanup=cleanup,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert cleanup_calls == [("feat/sm-checkout-127", 127)]
    assert report.cleaned_up == 1
    assert report.transitioned == 1


def test_reviewing_ci_red_does_not_invoke_cleanup(
    state_path: pathlib.Path,
) -> None:
    """CI red → building. Tree is NOT touched (Issue #127 scope)."""
    issues = [_make_issue(128, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 251, "url": "https://example/pr/251"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "deadbeef",
            "pr_url": "https://example/pr/251",
            "head_ref_name": "feat/red-128",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {"conclusion": "failure", "run_url": "https://example/ci/red"}

    def boom(_b: str | None, _n: int) -> None:
        raise AssertionError("cleanup must not run on CI-red path")

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: issues,
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        post_merge_cleanup=boom,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.cleaned_up == 0


def test_reviewing_pr_unmerged_does_not_invoke_cleanup(
    state_path: pathlib.Path,
) -> None:
    """PR closed-unmerged → stays at reviewing; cleanup never fires.

    The issue body explicitly carves this out: "keep the work for
    inspection" — we don't restore master if the PR didn't merge.
    """
    issues = [_make_issue(129, sm_labels=("sm:reviewing",))]

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 252, "url": "https://example/pr/252"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": False,
            "merge_commit_oid": None,
            "pr_url": None,
            "head_ref_name": None,
        }

    def boom(_b: str | None, _n: int) -> None:
        raise AssertionError("cleanup must not run on unmerged PR")

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        list_issues=lambda _r: issues,
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=lambda *_a, **_k: {"conclusion": None, "run_url": None},
        post_merge_cleanup=boom,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.cleaned_up == 0


def test_reviewing_done_cleanup_exception_is_swallowed(
    state_path: pathlib.Path,
) -> None:
    """A cleanup blow-up must not crash the dispatcher pass — the GH
    transition has already succeeded and we don't want to corrupt the
    audit trail on a tree-cleanup hiccup."""
    issues = [_make_issue(130, sm_labels=("sm:reviewing",))]
    logged: list[str] = []

    def find_pr(_repo: str, _n: int) -> dict | None:
        return {"number": 253, "url": "https://example/pr/253"}

    def merge_status(_repo: str, _pr: int) -> dict:
        return {
            "merged": True,
            "merge_commit_oid": "abc",
            "pr_url": "https://example/pr/253",
            "head_ref_name": "feat/x-130",
        }

    def ci(_repo: str, _sha: str) -> dict:
        return {"conclusion": "success", "run_url": "https://example/ci/g"}

    def cleanup(_b: str | None, _n: int) -> None:
        raise RuntimeError("disk full")

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_verify=False,
        list_issues=lambda _r: issues,
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=find_pr,
        pr_merge_status=merge_status,
        master_ci_status=ci,
        post_merge_cleanup=cleanup,
        now_iso=_frozen_now,
        log=logged.append,
    )

    assert exit_code == 0
    # Transition still counted (we did close the issue on GH).
    assert report.transitioned == 1
    # Cleanup NOT counted as a success.
    assert report.cleaned_up == 0
    assert any("post-merge cleanup raised" in m for m in logged)


def test_gh_get_pr_merge_status_extracts_head_ref_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PR-merge-status helper now returns ``head_ref_name`` so the
    cleanup helper knows which local branch to delete."""

    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        # Must request headRefName in the json field selector
        # (single comma-separated arg after ``--json``).
        json_idx = args.index("--json")
        assert "headRefName" in args[json_idx + 1].split(",")
        return json.dumps(
            {
                "state": "MERGED",
                "mergeCommit": {"oid": "abc123"},
                "url": "https://example/pr/9",
                "headRefName": "feat/x-9",
            }
        )

    monkeypatch.setattr(sm, "_run_gh", fake_run_gh)
    out = sm.gh_get_pr_merge_status("jcronq/alice", 9)
    assert out == {
        "merged": True,
        "merge_commit_oid": "abc123",
        "pr_url": "https://example/pr/9",
        "head_ref_name": "feat/x-9",
    }


# ---------------------------------------------------------------------------
# Issue #128 — sm:reviewing → sm:done verification gate
# ---------------------------------------------------------------------------


def _ci_green(_repo: str, _sha: str) -> dict:
    return {"conclusion": "success", "run_url": "https://example/ci/green"}


def _merge_status_merged(_repo: str, _pr: int) -> dict:
    return {
        "merged": True,
        "merge_commit_oid": "abc12345",
        "pr_url": "https://example/pr/200",
        "head_ref_name": "feat/x",
    }


def _find_pr_200(_repo: str, _n: int) -> dict | None:
    return {
        "number": 200,
        "url": "https://example/pr/200",
        "state": "MERGED",
    }


def test_verify_viewer_route_pass_passes_through(state_path: pathlib.Path) -> None:
    """A merged-green PR that touches viewer code AND probes successfully
    transitions to sm:done with both verify-pass and transition audit
    comments."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(200, sm_labels=("sm:reviewing",))]

    def pr_files(_repo: str, _n: int) -> list[str]:
        return ["src/alice_viewer/main.py", "tests/test_viewer_x.py"]

    def verifier(pr_number: int, files: list[str]) -> dict:
        assert pr_number == 200
        assert "src/alice_viewer/main.py" in files
        return {
            "outcome": "pass",
            "reason": "viewer marker present",
            "route": "http://localhost:7777/",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.verify_pass == 1
    assert report.verify_skip == 0
    assert report.verify_failed == 0
    assert report.transitioned == 1
    # Issue closed and relabeled.
    assert close_rec.closed == [("jcronq/alice", 200)]
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:done"]
    assert edit["remove"] == ["sm:reviewing"]
    bodies = [body for _r, _n, body in recorder.posted]
    assert any(
        b.startswith("[SM] verify-pass task=#200 route=http://localhost:7777/")
        for b in bodies
    )
    assert any(
        '[SM] transition from=reviewing to=done' in b for b in bodies
    )
    # verify-pass should be posted BEFORE the transition comment so the
    # audit trail reads in causal order.
    pass_idx = next(
        i for i, (_r, _n, b) in enumerate(recorder.posted)
        if b.startswith("[SM] verify-pass")
    )
    trans_idx = next(
        i for i, (_r, _n, b) in enumerate(recorder.posted)
        if "[SM] transition from=reviewing to=done" in b
    )
    assert pass_idx < trans_idx


def test_verify_skip_when_no_viewer_files_still_transitions(
    state_path: pathlib.Path,
) -> None:
    """A merged-green PR with no viewer touches gets verify-skip + still
    transitions to sm:done. The audit trail records that no recipe
    matched."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(201, sm_labels=("sm:reviewing",))]

    def pr_files(_repo: str, _n: int) -> list[str]:
        return ["src/alice_sm/dispatcher.py", "tests/test_sm_dispatcher.py"]

    # Verifier returns skip — its decision, not the dispatcher's.
    def verifier(_pr: int, _files: list[str]) -> dict:
        return {
            "outcome": "skip",
            "reason": "no verification recipe matched",
            "route": None,
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.verify_skip == 1
    assert report.verify_pass == 0
    assert report.verify_failed == 0
    assert report.transitioned == 1
    assert close_rec.closed == [("jcronq/alice", 201)]
    bodies = [body for _r, _n, body in recorder.posted]
    assert any(b.startswith("[SM] verify-skip task=#201") for b in bodies)


def test_verify_failed_halts_at_reviewing(state_path: pathlib.Path) -> None:
    """Verifier returns fail — issue stays at sm:reviewing, no label edit,
    no close, verify-failed comment posted."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(202, sm_labels=("sm:reviewing",))]

    def pr_files(_repo: str, _n: int) -> list[str]:
        return ["src/alice_viewer/main.py"]

    def verifier(_pr: int, _files: list[str]) -> dict:
        return {
            "outcome": "fail",
            "reason": "viewer probe HTTP 502",
            "route": "http://localhost:7777/",
        }

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda repo: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    # No transition, no close, no relabel.
    assert report.transitioned == 0
    assert close_rec.closed == []
    assert label_rec.calls == []
    # Verify-failed counted + comment posted.
    assert report.verify_failed == 1
    bodies = [body for _r, _n, body in recorder.posted]
    failed = [b for b in bodies if b.startswith("[SM] verify-failed task=#202")]
    assert len(failed) == 1
    assert 'reason="viewer probe HTTP 502"' in failed[0]
    # State persisted the dedup entry.
    persisted = json.loads(state_path.read_text())
    assert persisted["verify_failed_posted"] == [202]


def test_verify_failed_dedup_does_not_spam(state_path: pathlib.Path) -> None:
    """Second cadence on the same failing issue must NOT re-post the
    verify-failed comment — state-backed dedup."""
    issues = [_make_issue(203, sm_labels=("sm:reviewing",))]

    def pr_files(_repo: str, _n: int) -> list[str]:
        return ["src/alice_viewer/main.py"]

    def verifier(_pr: int, _files: list[str]) -> dict:
        return {"outcome": "fail", "reason": "viewer probe timeout", "route": None}

    # Pass 1: posts.
    recorder1 = Recorder()
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda r: issues,
        post_comment=recorder1,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert any(
        b.startswith("[SM] verify-failed") for _r, _n, b in recorder1.posted
    )

    # Pass 2: must not re-post.
    recorder2 = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda r: issues,
        post_comment=recorder2,
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    failed_bodies = [
        b for _r, _n, b in recorder2.posted if b.startswith("[SM] verify-failed")
    ]
    assert failed_bodies == []
    # Counter still increments — we did evaluate the verifier — but no
    # comment hit the wire. This is the "we know it's still broken,
    # waiting for Jason" cadence.
    assert report.verify_failed == 1


def test_verifier_exception_treated_as_failed(state_path: pathlib.Path) -> None:
    """A verifier that raises must NOT crash the dispatcher; it gets
    converted to a verify-failed outcome with the exception in the
    reason."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(204, sm_labels=("sm:reviewing",))]

    def pr_files(_repo: str, _n: int) -> list[str]:
        return ["src/alice_viewer/main.py"]

    def verifier(_pr: int, _files: list[str]) -> dict:
        raise RuntimeError("network on fire")

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda r: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.verify_failed == 1
    assert report.transitioned == 0
    assert close_rec.closed == []
    bodies = [b for _r, _n, b in recorder.posted]
    assert any(
        b.startswith("[SM] verify-failed") and "network on fire" in b for b in bodies
    )


def test_verify_disabled_via_kwarg_skips_gate(state_path: pathlib.Path) -> None:
    """``enable_verify=False`` reverts to pre-#128 behavior: CI-green
    leads straight to sm:done with NO verify-* audit comment."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(205, sm_labels=("sm:reviewing",))]

    # If the verifier or pr_files were called we'd see it — leave the
    # production helpers wired in but flip the kill switch and assert
    # neither runs.
    sentinel: list[str] = []

    def pr_files(_repo: str, _n: int) -> list[str]:
        sentinel.append("pr_files")
        return ["src/alice_viewer/main.py"]

    def verifier(_pr: int, _files: list[str]) -> dict:
        sentinel.append("verifier")
        return {"outcome": "fail", "reason": "should not run", "route": None}

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda r: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert sentinel == []
    assert report.transitioned == 1
    assert close_rec.closed == [("jcronq/alice", 205)]
    bodies = [b for _r, _n, b in recorder.posted]
    assert not any(b.startswith("[SM] verify-") for b in bodies)


def test_verify_recovery_clears_state_ledger(state_path: pathlib.Path) -> None:
    """Once the verifier flips back to pass, the verify_failed_posted
    ledger entry for that issue is cleared, so a future re-failure on
    the same issue (e.g. CI red → re-green → fail again) gets a fresh
    comment."""
    issues = [_make_issue(206, sm_labels=("sm:reviewing",))]
    outcomes = iter([
        {"outcome": "fail", "reason": "first fail", "route": "http://x/"},
        {"outcome": "pass", "reason": "recovered", "route": "http://x/"},
    ])

    def pr_files(_repo: str, _n: int) -> list[str]:
        return ["src/alice_viewer/main.py"]

    def verifier(_pr: int, _files: list[str]) -> dict:
        return next(outcomes)

    # Pass 1: fail → ledger populated.
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda r: issues,
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=CloseRecorder(),
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert json.loads(state_path.read_text())["verify_failed_posted"] == [206]

    # Pass 2: recovers → ledger cleared, issue transitions.
    close2 = CloseRecorder()
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda r: issues,
        post_comment=Recorder(),
        edit_labels=LabelRecorder(),
        close_issue=close2,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert close2.closed == [("jcronq/alice", 206)]
    assert json.loads(state_path.read_text())["verify_failed_posted"] == []


def test_verify_viewer_route_marker_missing_is_fail() -> None:
    """The viewer-route smoke test fails when the response body doesn't
    contain the marker substring, even with a 200 status."""

    def fake_get(_url: str) -> tuple[int, str]:
        return 200, "<html><body>oops, wrong page</body>"

    verdict = sm.verify_viewer_route(
        url="http://x/", marker="</html>", http_get=fake_get
    )
    assert verdict["outcome"] == "fail"
    assert "marker" in verdict["reason"]
    assert verdict["route"] == "http://x/"


def test_verify_viewer_route_marker_present_is_pass() -> None:
    def fake_get(_url: str) -> tuple[int, str]:
        return 200, "<html><body>ok</body></html>"

    verdict = sm.verify_viewer_route(
        url="http://x/", marker="</html>", http_get=fake_get
    )
    assert verdict["outcome"] == "pass"
    assert verdict["route"] == "http://x/"


def test_verify_viewer_route_http_500_is_fail() -> None:
    def fake_get(_url: str) -> tuple[int, str]:
        return 500, "Internal Server Error"

    verdict = sm.verify_viewer_route(
        url="http://x/", marker="</html>", http_get=fake_get
    )
    assert verdict["outcome"] == "fail"
    assert "HTTP 500" in verdict["reason"]


def test_verify_viewer_route_connection_refused_is_fail() -> None:
    import urllib.error

    def fake_get(_url: str) -> tuple[int, str]:
        raise urllib.error.URLError("Connection refused")

    verdict = sm.verify_viewer_route(
        url="http://x/", marker="</html>", http_get=fake_get
    )
    assert verdict["outcome"] == "fail"
    assert "viewer probe failed" in verdict["reason"]


def test_default_verifier_skips_when_no_viewer_files() -> None:
    verdict = sm.default_verifier(
        99,
        ["src/alice_sm/dispatcher.py"],
        viewer_url="http://unused/",
        viewer_marker="</html>",
        http_get=lambda _u: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    assert verdict["outcome"] == "skip"
    assert verdict["route"] is None


def test_default_verifier_runs_recipe_when_viewer_files_present() -> None:
    called: list[str] = []

    def fake_get(url: str) -> tuple[int, str]:
        called.append(url)
        return 200, "<html>ok</html>"

    verdict = sm.default_verifier(
        99,
        ["src/alice_viewer/templates/timeline.html"],
        viewer_url="http://probe/",
        viewer_marker="</html>",
        http_get=fake_get,
    )
    assert verdict["outcome"] == "pass"
    assert called == ["http://probe/"]


def test_verify_env_kill_switch_disables_default_verifier(
    state_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ALICE_VERIFY_ENABLED=0`` reverts to pre-#128 behavior even when
    the caller didn't pass ``enable_verify=False`` (operational
    kill-switch the dispatcher checks each pass)."""
    monkeypatch.setenv(sm.VERIFY_ENABLED_ENV, "0")

    recorder = Recorder()
    label_rec = LabelRecorder()
    close_rec = CloseRecorder()
    issues = [_make_issue(207, sm_labels=("sm:reviewing",))]
    sentinel: list[str] = []

    def pr_files(_repo: str, _n: int) -> list[str]:
        sentinel.append("pr_files")
        return ["src/alice_viewer/main.py"]

    def verifier(_pr: int, _files: list[str]) -> dict:
        sentinel.append("verifier")
        return {"outcome": "fail", "reason": "x", "route": None}

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        list_issues=lambda r: issues,
        post_comment=recorder,
        edit_labels=label_rec,
        close_issue=close_rec,
        find_linked_pr=_find_pr_200,
        pr_merge_status=_merge_status_merged,
        master_ci_status=_ci_green,
        pr_files=pr_files,
        verify_pr=verifier,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert sentinel == []  # neither helper ran
    assert report.transitioned == 1
    assert close_rec.closed == [("jcronq/alice", 207)]


def test_render_verify_comment_shapes() -> None:
    ts = "2026-05-12T12:00:00+00:00"
    assert (
        sm.render_verify_comment("pass", 1, route="http://x/", timestamp=ts)
        == "[SM] verify-pass task=#1 route=http://x/ ts=2026-05-12T12:00:00+00:00"
    )
    assert (
        sm.render_verify_comment("skip", 2, reason="no recipe", timestamp=ts)
        == '[SM] verify-skip task=#2 reason="no recipe" '
           "ts=2026-05-12T12:00:00+00:00"
    )
    assert (
        sm.render_verify_comment("failed", 3, reason="500", timestamp=ts)
        == '[SM] verify-failed task=#3 reason="500" '
           "ts=2026-05-12T12:00:00+00:00"
    )


def test_dispatcher_state_carries_verify_failed_field_across_load_save(
    state_path: pathlib.Path,
) -> None:
    """Forward-compatible state file: missing ``verify_failed_posted``
    key in an older file loads as empty; new key persists on save."""
    state_path.write_text(
        json.dumps({"version": sm.STATE_VERSION, "hello_commented": [1, 2]})
    )
    loaded = sm.load_state(state_path)
    assert loaded.hello_commented == [1, 2]
    assert loaded.verify_failed_posted == []

    loaded.mark_verify_failed(42)
    sm.save_state(state_path, loaded)
    on_disk = json.loads(state_path.read_text())
    assert on_disk["verify_failed_posted"] == [42]
    assert on_disk["hello_commented"] == [1, 2]


def test_gh_get_pr_files_parses_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_gh(args: list[str], *, timeout: int = 60) -> str:
        assert "files" in args[args.index("--json") + 1].split(",")
        return json.dumps({
            "files": [
                {"path": "src/alice_viewer/main.py"},
                {"path": "tests/test_x.py"},
            ]
        })

    monkeypatch.setattr(sm, "_run_gh", fake_run_gh)
    out = sm.gh_get_pr_files("jcronq/alice", 200)
    assert out == ["src/alice_viewer/main.py", "tests/test_x.py"]


# ---------------------------------------------------------------------------
# Issue #157 — sm:needs_study handler
# ---------------------------------------------------------------------------


def _audit_comment(body: str, *, author: str = "jcronq") -> dict:
    """Helper: build a ``gh issue view --json comments`` entry."""
    return {"body": body, "author": {"login": author}}


def _needs_study_issue(
    number: int,
    *,
    body: str = "study this thing",
    art_labels: tuple[str, ...] = ("art:code",),
    author: str = "jcronq",
    title: str = "Study task",
) -> dict:
    return _make_issue(
        number,
        author=author,
        sm_labels=("sm:needs_study",),
        art_labels=art_labels,
        title=title,
    ) | {"body": body}


def test_needs_study_writes_hint_and_posts_audit_comment(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """First pass: write the hint file + post the study-hint-written audit."""
    recorder = Recorder()
    label_rec = LabelRecorder()
    issues = [_needs_study_issue(300, body="please study X")]
    notes_dir = tmp_path / "notes"

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: [],
        post_comment=recorder,
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.hinted == 1
    assert report.transitioned == 0
    # Hint file exists with the expected name.
    hint_file = notes_dir / "sm-needs-study-issue300.md"
    assert hint_file.is_file()
    contents = hint_file.read_text()
    assert "kind: sm-needs-study" in contents
    assert "issue: 300" in contents
    assert "please study X" in contents
    # Exactly one comment posted: the audit.
    assert len(recorder.posted) == 1
    _repo, num, body = recorder.posted[0]
    assert num == 300
    assert body.startswith("[SM] study-hint-written task=#300")
    assert str(hint_file) in body
    # State ledger updated.
    persisted = json.loads(state_path.read_text())
    assert persisted["needs_study_hinted"] == [300]


def test_needs_study_hint_is_idempotent_across_passes(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Second pass with the same slate must not re-write or re-post."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(301)]

    # First pass — write + post.
    rec1 = Recorder()
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: [],
        post_comment=rec1,
        edit_labels=LabelRecorder(),
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert len(rec1.posted) == 1

    hint_file = notes_dir / "sm-needs-study-issue301.md"
    first_contents = hint_file.read_text()

    # Second pass — ledger says we've already hinted.
    rec2 = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: [],
        post_comment=rec2,
        edit_labels=LabelRecorder(),
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert exit_code == 0
    assert rec2.posted == []
    assert report.hinted == 0
    assert hint_file.read_text() == first_contents


def test_needs_study_skips_hint_when_audit_comment_already_exists(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """State-file reset: ledger empty but the audit comment is on GH."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(302)]
    existing_audit = _audit_comment(
        "[SM] study-hint-written task=#302 path=/tmp/x.md ts=2026-05-12T00:00:00Z"
    )

    recorder = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: [existing_audit],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert exit_code == 0
    assert recorder.posted == []
    assert report.hinted == 0
    assert not (notes_dir / "sm-needs-study-issue302.md").exists()
    persisted = json.loads(state_path.read_text())
    assert 302 in persisted["needs_study_hinted"]


def test_needs_study_audit_from_untrusted_author_does_not_dedup(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A drive-by commenter pasting the audit prefix mustn't suppress the hint."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(303)]
    forged = _audit_comment(
        "[SM] study-hint-written task=#303 path=/forged.md ts=now",
        author="random-drive-by",
    )

    recorder = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: [forged],
        post_comment=recorder,
        edit_labels=LabelRecorder(),
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )
    assert exit_code == 0
    assert report.hinted == 1
    assert (notes_dir / "sm-needs-study-issue303.md").is_file()
    assert any(b.startswith("[SM] study-hint-written") for _r, _n, b in recorder.posted)


def test_needs_study_study_complete_transitions_to_selected(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(310, art_labels=("art:code",))]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#310 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment(
            "[SM] study-complete art=art:code findings=[[research-310]]"
        ),
    ]

    recorder = Recorder()
    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=recorder,
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.transitioned == 1
    assert (310, "sm:needs_study", "sm:selected") in report.transitions
    transition_bodies = [b for _r, _n, b in recorder.posted if "transition" in b]
    assert any(
        "from=needs_study to=selected" in b and "study-complete" in b
        for b in transition_bodies
    )
    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:selected"]
    assert edit["remove"] == ["sm:needs_study"]


def test_needs_study_study_complete_swaps_art_label(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """If thinking concludes the artifact kind changed, swap art:* atomically."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(311, art_labels=("art:code",))]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#311 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment(
            "[SM] study-complete art=art:research_note findings=[[r311]]"
        ),
    ]

    label_rec = LabelRecorder()
    sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=Recorder(),
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert len(label_rec.calls) == 1
    edit = label_rec.calls[0]
    assert set(edit["add"]) == {"sm:selected", "art:research_note"}
    assert set(edit["remove"]) == {"sm:needs_study", "art:code"}


def test_needs_study_study_complete_unknown_art_logs_and_does_not_transition(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """An unrecognized art label is rejected by the parser → no transition."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(312)]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#312 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment("[SM] study-complete art=art:bogus findings=[[r312]]"),
    ]
    logged: list[str] = []

    label_rec = LabelRecorder()
    recorder = Recorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=recorder,
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=logged.append,
    )

    assert exit_code == 0
    assert report.transitioned == 0
    assert label_rec.calls == []
    assert any("art:bogus" in m and "whitelist" in m for m in logged)


def test_needs_study_study_blocked_transitions_to_blocked(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(320)]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#320 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment('[SM] study-blocked reason="need vault access"'),
    ]

    recorder = Recorder()
    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=recorder,
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.transitioned == 1
    assert (320, "sm:needs_study", "sm:blocked") in report.transitions
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:blocked"]
    assert edit["remove"] == ["sm:needs_study"]


def test_needs_study_study_rejected_transitions_to_rejected(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(321)]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#321 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment('[SM] study-rejected reason="out of scope"'),
    ]

    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=Recorder(),
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.transitioned == 1
    assert (321, "sm:needs_study", "sm:rejected") in report.transitions
    edit = label_rec.calls[0]
    assert edit["add"] == ["sm:rejected"]
    assert edit["remove"] == ["sm:needs_study"]


def test_needs_study_study_progress_logs_and_stays(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Most recent comment is study-progress → no transition, log once."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(322)]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#322 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment("[SM] study-progress note=[[checkpoint-322]]"),
    ]

    logged: list[str] = []
    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=Recorder(),
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=logged.append,
    )

    assert exit_code == 0
    assert report.transitioned == 0
    assert label_rec.calls == []
    assert any("thinking still working" in m for m in logged)


def test_needs_study_picks_most_recent_study_comment(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Comments are scanned newest-first: study-complete after study-progress
    wins."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(323)]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#323 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment("[SM] study-progress note=[[chk1]]"),
        _audit_comment("[SM] study-progress note=[[chk2]]"),
        _audit_comment("[SM] study-complete art=art:code findings=[[done]]"),
    ]

    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=Recorder(),
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.transitioned == 1
    assert (323, "sm:needs_study", "sm:selected") in report.transitions


def test_needs_study_progress_after_complete_does_not_unwind(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """If thinking posts study-progress AFTER a study-complete (out of
    spec but observable), the newest verb wins → no transition this pass."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(324)]
    comments = [
        _audit_comment("[SM] study-complete art=art:code findings=[[done]]"),
        _audit_comment("[SM] study-progress note=[[oops]]"),
    ]

    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=Recorder(),
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.transitioned == 0
    assert label_rec.calls == []


def test_needs_study_untrusted_comment_author_is_ignored(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """An untrusted commenter cannot trigger a transition by pasting the
    study-complete verb."""
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(325)]
    comments = [
        _audit_comment(
            "[SM] study-hint-written task=#325 path=/x.md ts=2026-05-12T00:00:00Z"
        ),
        _audit_comment(
            "[SM] study-complete art=art:code findings=[[fake]]",
            author="random-drive-by",
        ),
    ]

    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: comments,
        post_comment=Recorder(),
        edit_labels=label_rec,
        notes_dir=notes_dir,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.transitioned == 0
    assert label_rec.calls == []


def test_needs_study_dry_run_does_not_write_or_post(
    state_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    notes_dir = tmp_path / "notes"
    issues = [_needs_study_issue(330)]

    recorder = Recorder()
    label_rec = LabelRecorder()
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        list_issues=lambda repo: issues,
        list_comments=lambda repo, n: [],
        post_comment=recorder,
        edit_labels=label_rec,
        notes_dir=notes_dir,
        dry_run=True,
        now_iso=_frozen_now,
        log=lambda _m: None,
    )

    assert exit_code == 0
    assert report.hinted == 1  # intent recorded
    assert recorder.posted == []
    assert label_rec.calls == []
    assert not (notes_dir / "sm-needs-study-issue330.md").exists()
    # Dry-run also skips state persistence.
    assert not state_path.is_file()


def test_render_study_hint_audit_shape() -> None:
    out = sm.render_study_hint_audit_comment(
        42,
        "/tmp/hint.md",
        timestamp="2026-05-12T12:00:00+00:00",
    )
    assert out == (
        "[SM] study-hint-written task=#42 path=/tmp/hint.md "
        "ts=2026-05-12T12:00:00+00:00"
    )


def test_dispatcher_state_carries_needs_study_field_across_load_save(
    state_path: pathlib.Path,
) -> None:
    """Forward-compat: pre-#157 state files load with empty ledger; new
    field persists on save."""
    state_path.write_text(
        json.dumps({"version": sm.STATE_VERSION, "hello_commented": [1, 2]})
    )
    loaded = sm.load_state(state_path)
    assert loaded.hello_commented == [1, 2]
    assert loaded.needs_study_hinted == []

    loaded.mark_needs_study_hint(42)
    loaded.mark_needs_study_hint(43)
    sm.save_state(state_path, loaded)
    on_disk = json.loads(state_path.read_text())
    assert on_disk["needs_study_hinted"] == [42, 43]
    assert on_disk["hello_commented"] == [1, 2]


def test_dispatcher_state_needs_study_fifo_eviction() -> None:
    state = sm.DispatcherState()
    for n in range(1, sm.SEEN_ISSUE_CAP + 1):
        state.mark_needs_study_hint(n)
    assert len(state.needs_study_hinted) == sm.SEEN_ISSUE_CAP
    state.mark_needs_study_hint(sm.SEEN_ISSUE_CAP + 1)
    assert len(state.needs_study_hinted) == sm.SEEN_ISSUE_CAP
    assert 1 not in state.needs_study_hinted
    assert state.needs_study_hinted[-1] == sm.SEEN_ISSUE_CAP + 1
