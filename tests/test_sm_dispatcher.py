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
