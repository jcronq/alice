"""Unit tests for :mod:`alice_forge.github_reconciler`.

Covers the reconciliation contract from issue #376:

* New issue with sm:* label → task created and walked to the derived status.
* Label change on an already-tracked issue → transition emitted.
* No-op on rerun with unchanged GitHub state (idempotency).
* PR merge → ``done`` with merge_ref.
* Closed without merge → ``rejected``.
* Unknown sm:* label → warning, no transition.
* Multiple sm:* labels → most-advanced wins.
* Three-run integration scenario covering create → transition →
  finalize.

The reconciler does not call the real ``gh`` CLI in tests — every test
passes a ``gh_runner`` closure that returns a pre-built JSON payload.
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest

from alice_forge.github_reconciler import (
    DEFAULT_LOOKBACK_DAYS,
    LABEL_TO_STATUS,
    derive_target_status,
    fetch_recent_issues,
    find_task_for_issue,
    issue_tag,
    reconcile,
    reconcile_issue,
    shortest_path,
)
from alice_forge.task_store import TaskStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue(
    number: int,
    *,
    state: str = "open",
    labels: list[str] | None = None,
    title: str = "",
    pull_request: dict | None = None,
    html_url: str | None = None,
) -> dict:
    """Build a minimal issue payload matching the gh api shape."""
    return {
        "number": number,
        "title": title or f"Issue {number}",
        "state": state,
        "labels": [{"name": l} for l in (labels or [])],
        "html_url": html_url or f"https://github.com/jcronq/alice/issues/{number}",
        "pull_request": pull_request,
    }


def _make_gh_runner(payloads: dict[str, list[dict]]) -> callable:
    """Return a ``gh_runner`` callable that hands out canned payloads.

    Keyed by repo. Page 1 returns the full list; subsequent pages
    return ``[]`` so pagination short-circuits.
    """

    def runner(args: list[str]) -> str:
        # args looks like ["api", "repos/<repo>/issues?state=all&since=...&per_page=100&page=N"]
        endpoint = args[-1]
        repo = endpoint.split("repos/")[1].split("/issues")[0]
        if "page=1&" in endpoint or endpoint.endswith("page=1"):
            return json.dumps(payloads.get(repo, []))
        return "[]"

    return runner


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks")


# ---------------------------------------------------------------------------
# derive_target_status
# ---------------------------------------------------------------------------


def test_derive_open_with_sm_draft() -> None:
    issue = _issue(1, labels=["sm:draft", "art:code"])
    status, merge_ref = derive_target_status(issue)
    assert status == "draft"
    assert merge_ref is None


def test_derive_open_with_sm_selected_wins_over_sm_draft() -> None:
    """Most-advanced label wins."""
    issue = _issue(1, labels=["sm:draft", "sm:selected"])
    status, _ = derive_target_status(issue)
    assert status == "selected"


def test_derive_open_with_sm_done_wins_over_sm_building() -> None:
    issue = _issue(1, labels=["sm:building", "sm:done"])
    status, _ = derive_target_status(issue)
    assert status == "done"


def test_derive_closed_merged_pr_returns_done_with_merge_ref() -> None:
    issue = _issue(
        1,
        state="closed",
        labels=["sm:reviewing"],
        pull_request={"merged_at": "2026-05-26T14:14:24Z"},
        html_url="https://github.com/jcronq/alice/pull/1",
    )
    status, merge_ref = derive_target_status(issue)
    assert status == "done"
    assert merge_ref == "https://github.com/jcronq/alice/pull/1"


def test_derive_closed_unmerged_pr_returns_rejected() -> None:
    issue = _issue(
        1,
        state="closed",
        labels=["sm:reviewing"],
        pull_request={"merged_at": None},
    )
    status, merge_ref = derive_target_status(issue)
    assert status == "rejected"
    assert merge_ref is None


def test_derive_closed_issue_without_pr_returns_rejected() -> None:
    issue = _issue(1, state="closed", labels=["sm:building"])
    status, _ = derive_target_status(issue)
    assert status == "rejected"


def test_derive_no_sm_label_returns_none() -> None:
    issue = _issue(1, labels=["art:code", "bug"])
    status, _ = derive_target_status(issue)
    assert status is None


def test_derive_unknown_sm_label_returns_none(caplog) -> None:
    """Typo'd sm:* labels log a warning and do not promote."""
    issue = _issue(1, labels=["sm:bogus-typo"])
    with caplog.at_level(logging.WARNING):
        status, _ = derive_target_status(issue)
    assert status is None
    assert any("sm:bogus-typo" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# shortest_path
# ---------------------------------------------------------------------------


def test_shortest_path_one_hop() -> None:
    assert shortest_path(
        "draft",
        "selected",
        have_merge_ref=False,
        have_validation_evidence=False,
        have_unblocked_by=False,
    ) == ["selected"]


def test_shortest_path_multi_hop_chain() -> None:
    """draft → done (no merge_ref) chains through reviewing."""
    path = shortest_path(
        "draft",
        "done",
        have_merge_ref=False,
        have_validation_evidence=False,
        have_unblocked_by=False,
    )
    assert path is not None
    assert path[-1] == "done"
    assert "reviewing" in path


def test_shortest_path_building_to_done_with_merge_ref_is_one_hop() -> None:
    path = shortest_path(
        "building",
        "done",
        have_merge_ref=True,
        have_validation_evidence=False,
        have_unblocked_by=False,
    )
    assert path == ["done"]


def test_shortest_path_building_to_done_without_merge_ref_chains_via_reviewing() -> None:
    path = shortest_path(
        "building",
        "done",
        have_merge_ref=False,
        have_validation_evidence=False,
        have_unblocked_by=False,
    )
    assert path == ["reviewing", "done"]


def test_shortest_path_to_blocked_requires_unblocked_by() -> None:
    assert (
        shortest_path(
            "building",
            "blocked",
            have_merge_ref=False,
            have_validation_evidence=False,
            have_unblocked_by=False,
        )
        is None
    )
    assert shortest_path(
        "building",
        "blocked",
        have_merge_ref=False,
        have_validation_evidence=False,
        have_unblocked_by=True,
    ) == ["blocked"]


def test_shortest_path_terminal_source_returns_none() -> None:
    assert (
        shortest_path(
            "done",
            "rejected",
            have_merge_ref=False,
            have_validation_evidence=False,
            have_unblocked_by=False,
        )
        is None
    )


def test_shortest_path_same_state_returns_empty() -> None:
    assert (
        shortest_path(
            "draft",
            "draft",
            have_merge_ref=False,
            have_validation_evidence=False,
            have_unblocked_by=False,
        )
        == []
    )


# ---------------------------------------------------------------------------
# reconcile_issue — single issue cases
# ---------------------------------------------------------------------------


def test_reconcile_creates_task_from_new_issue(store: TaskStore) -> None:
    issue = _issue(49, labels=["sm:draft", "art:code"], title="Auto-fix candidate")
    counters = reconcile_issue(store, "jcronq/cozyhem-engine", issue)
    assert counters["created"] == 1
    # Walked from initial draft to draft target → zero transitions.
    assert counters["transitions"] == 0
    found = find_task_for_issue(
        store, issue_tag("jcronq/cozyhem-engine", 49)
    )
    assert found is not None
    assert found["status"] == "draft"
    # Tags include the canonical (repo, issue#) pair plus the art label
    # and the github-synced marker.
    tags = set(found["tags"])
    assert "jcronq/cozyhem-engine" in tags
    assert "github-synced" in tags
    assert "github-issue:jcronq/cozyhem-engine#49" in tags
    assert "art:code" in tags


def test_reconcile_creates_and_walks_to_sm_selected(store: TaskStore) -> None:
    """A fresh issue carrying sm:selected lands as selected after one pass."""
    issue = _issue(37, labels=["sm:selected", "art:code"])
    counters = reconcile_issue(store, "jcronq/cozyhem-engine", issue)
    assert counters["created"] == 1
    assert counters["transitions"] == 1
    found = find_task_for_issue(
        store, issue_tag("jcronq/cozyhem-engine", 37)
    )
    assert found is not None
    assert found["status"] == "selected"


def test_reconcile_creates_and_walks_to_sm_reviewing(store: TaskStore) -> None:
    """sm:needs_study → reviewing (the mapped status) via the canonical path."""
    issue = _issue(31, labels=["sm:needs_study", "art:code"])
    counters = reconcile_issue(store, "jcronq/cozyhem-engine", issue)
    assert counters["created"] == 1
    found = find_task_for_issue(
        store, issue_tag("jcronq/cozyhem-engine", 31)
    )
    assert found is not None
    assert found["status"] == "reviewing"


def test_reconcile_label_change_emits_transition(store: TaskStore) -> None:
    """Once a task exists, a label promotion appends a single transition."""
    # Pass 1: sm:selected.
    reconcile_issue(
        store, "jcronq/alice", _issue(100, labels=["sm:selected"])
    )
    # Pass 2: same issue now sm:building.
    counters = reconcile_issue(
        store, "jcronq/alice", _issue(100, labels=["sm:building"])
    )
    assert counters["created"] == 0
    assert counters["transitions"] == 1
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 100))
    assert found is not None
    assert found["status"] == "building"


def test_reconcile_no_op_when_label_matches_status(store: TaskStore) -> None:
    """Re-running with unchanged labels produces zero transitions."""
    reconcile_issue(
        store, "jcronq/alice", _issue(101, labels=["sm:selected"])
    )
    counters = reconcile_issue(
        store, "jcronq/alice", _issue(101, labels=["sm:selected"])
    )
    assert counters["created"] == 0
    assert counters["transitions"] == 0


def test_reconcile_closed_merged_pr_transitions_done_with_merge_ref(
    store: TaskStore,
) -> None:
    # Pass 1: sm:reviewing, open.
    reconcile_issue(
        store, "jcronq/alice", _issue(200, labels=["sm:building"])
    )
    # Pass 2: PR merged.
    counters = reconcile_issue(
        store,
        "jcronq/alice",
        _issue(
            200,
            state="closed",
            labels=["sm:reviewing"],
            pull_request={"merged_at": "2026-05-26T15:00:00Z"},
            html_url="https://github.com/jcronq/alice/pull/200",
        ),
    )
    assert counters["transitions"] >= 1
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 200))
    assert found is not None
    assert found["status"] == "done"
    # Verify merge_ref ended up on the task.yaml.
    record = store.load(found["id"])
    assert record.merge_ref == "https://github.com/jcronq/alice/pull/200"


def test_reconcile_closed_unmerged_transitions_to_rejected(
    store: TaskStore,
) -> None:
    reconcile_issue(
        store, "jcronq/alice", _issue(201, labels=["sm:selected"])
    )
    counters = reconcile_issue(
        store,
        "jcronq/alice",
        _issue(
            201,
            state="closed",
            labels=["sm:selected"],
            pull_request={"merged_at": None},
        ),
    )
    assert counters["transitions"] >= 1
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 201))
    assert found is not None
    assert found["status"] == "rejected"


def test_reconcile_unknown_sm_label_is_skipped(
    store: TaskStore, caplog
) -> None:
    """Typo'd sm:* label produces no task and logs a warning."""
    issue = _issue(300, labels=["sm:bogus-typo"])
    with caplog.at_level(logging.WARNING):
        counters = reconcile_issue(store, "jcronq/alice", issue)
    assert counters["created"] == 0
    assert counters["transitions"] == 0
    assert counters["skipped"] == 1
    assert any("sm:bogus-typo" in r.message for r in caplog.records)


def test_reconcile_multiple_sm_labels_picks_most_advanced(
    store: TaskStore,
) -> None:
    """sm:draft + sm:building on the same issue → builds task at building."""
    issue = _issue(400, labels=["sm:draft", "sm:building", "art:code"])
    counters = reconcile_issue(store, "jcronq/alice", issue)
    assert counters["created"] == 1
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 400))
    assert found is not None
    assert found["status"] == "building"


def test_reconcile_terminal_local_task_with_diverged_target_logs_skip(
    store: TaskStore, caplog
) -> None:
    """If the local task is already done/rejected and GH state implies a
    non-terminal target, the reconciler logs and skips rather than
    trying an impossible transition."""
    # Build a task and drive it to done.
    pass1 = _issue(
        500,
        state="closed",
        labels=["sm:reviewing"],
        pull_request={"merged_at": "2026-05-26T15:00:00Z"},
        html_url="https://github.com/jcronq/alice/pull/500",
    )
    reconcile_issue(store, "jcronq/alice", pass1)
    # Now simulate the issue being reopened with sm:draft.
    pass2 = _issue(500, state="open", labels=["sm:draft"])
    with caplog.at_level(logging.WARNING):
        counters = reconcile_issue(store, "jcronq/alice", pass2)
    assert counters["transitions"] == 0
    # Existing terminal task isn't moved.
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 500))
    assert found is not None
    assert found["status"] == "done"


# ---------------------------------------------------------------------------
# reconcile — multi-issue, multi-repo
# ---------------------------------------------------------------------------


def test_reconcile_multi_repo_rolls_up_counters(store: TaskStore) -> None:
    runner = _make_gh_runner(
        {
            "jcronq/cozyhem-engine": [
                _issue(49, labels=["sm:draft"]),
                _issue(37, labels=["sm:selected"]),
            ],
            "jcronq/alice": [
                _issue(370, labels=["sm:building"]),
            ],
        }
    )
    rolled = reconcile(
        ["jcronq/cozyhem-engine", "jcronq/alice"],
        store=store,
        gh_runner=runner,
    )
    assert rolled["jcronq/cozyhem-engine"]["created"] == 2
    assert rolled["jcronq/alice"]["created"] == 1
    assert rolled["__total__"]["created"] == 3
    assert rolled["__total__"]["issues_seen"] == 3


def test_reconcile_is_idempotent_on_unchanged_state(store: TaskStore) -> None:
    """Running the reconciler twice over the same payload produces no
    new transitions on the second pass."""
    payloads = {
        "jcronq/alice": [
            _issue(600, labels=["sm:building"]),
            _issue(601, labels=["sm:selected"]),
        ]
    }
    runner = _make_gh_runner(payloads)
    rolled_a = reconcile(["jcronq/alice"], store=store, gh_runner=runner)
    rolled_b = reconcile(["jcronq/alice"], store=store, gh_runner=runner)
    assert rolled_a["__total__"]["created"] == 2
    assert rolled_b["__total__"]["created"] == 0
    assert rolled_b["__total__"]["transitions"] == 0


def test_reconcile_three_run_lifecycle(store: TaskStore) -> None:
    """Integration-shaped: simulate three runs with evolving GH state and
    verify the on-disk task store ends in the expected shape.

    Run 1: issue opens with sm:draft → task in draft.
    Run 2: label flips to sm:selected → transition emitted.
    Run 3: PR merges and closes → task in done with merge_ref.
    """
    # Run 1
    runner_a = _make_gh_runner(
        {"jcronq/alice": [_issue(700, labels=["sm:draft", "art:code"])]}
    )
    reconcile(["jcronq/alice"], store=store, gh_runner=runner_a)
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 700))
    assert found is not None
    assert found["status"] == "draft"

    # Run 2
    runner_b = _make_gh_runner(
        {"jcronq/alice": [_issue(700, labels=["sm:selected", "art:code"])]}
    )
    reconcile(["jcronq/alice"], store=store, gh_runner=runner_b)
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 700))
    assert found is not None
    assert found["status"] == "selected"

    # Run 3: closed-merged PR
    runner_c = _make_gh_runner(
        {
            "jcronq/alice": [
                _issue(
                    700,
                    state="closed",
                    labels=["sm:building", "art:code"],
                    pull_request={"merged_at": "2026-05-27T10:00:00Z"},
                    html_url="https://github.com/jcronq/alice/pull/700",
                )
            ]
        }
    )
    reconcile(["jcronq/alice"], store=store, gh_runner=runner_c)
    found = find_task_for_issue(store, issue_tag("jcronq/alice", 700))
    assert found is not None
    assert found["status"] == "done"
    record = store.load(found["id"])
    assert record.merge_ref == "https://github.com/jcronq/alice/pull/700"

    # Run 4: nothing changes → no new transitions.
    rolled = reconcile(["jcronq/alice"], store=store, gh_runner=runner_c)
    assert rolled["__total__"]["transitions"] == 0
    assert rolled["__total__"]["created"] == 0


# ---------------------------------------------------------------------------
# fetch_recent_issues
# ---------------------------------------------------------------------------


def test_fetch_recent_issues_paginates_until_short_page() -> None:
    """Pagination stops as soon as a page returns fewer than PAGE_SIZE
    results."""
    calls: list[str] = []

    def runner(args: list[str]) -> str:
        calls.append(args[-1])
        # Always return one issue on page 1, zero on subsequent pages.
        if "page=1" in args[-1]:
            return json.dumps([_issue(1, labels=["sm:draft"])])
        return "[]"

    out = fetch_recent_issues(
        "jcronq/alice",
        lookback_days=DEFAULT_LOOKBACK_DAYS,
        gh_runner=runner,
    )
    assert len(out) == 1
    # Should have stopped after page 1 (length < PAGE_SIZE).
    assert len(calls) == 1


def test_fetch_recent_issues_handles_malformed_json() -> None:
    def runner(args: list[str]) -> str:
        return "not-json"

    out = fetch_recent_issues(
        "jcronq/alice",
        lookback_days=DEFAULT_LOOKBACK_DAYS,
        gh_runner=runner,
    )
    assert out == []


# ---------------------------------------------------------------------------
# LABEL_TO_STATUS sanity
# ---------------------------------------------------------------------------


def test_label_to_status_covers_canonical_sm_labels() -> None:
    """Every SM v2 label mentioned in the design must map somewhere."""
    expected = {
        "sm:draft",
        "sm:needs_study",
        "sm:reviewing",
        "sm:selected",
        "sm:building",
        "sm:validating",
        "sm:done",
        "sm:rejected",
        "sm:blocked",
    }
    assert set(LABEL_TO_STATUS) == expected
