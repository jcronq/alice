"""Tests for ``scripts/backfill_sm_rejected_784.py``.

The backfill script audits closed ``sm:rejected`` issues against their
linked PRs. If a PR actually merged, the sweeper's pre-#784 CI-red
inheritance bug mis-labelled it; the backfill flips it back to
``sm:done``.

We don't exercise the real ``gh`` CLI — the ``runner`` injection point
lets the tests stub exact command/response pairs.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest


SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def backfill_module():
    """Import the script by path so tests don't depend on PYTHONPATH."""
    if str(SCRIPT_PATH) not in sys.path:
        sys.path.insert(0, str(SCRIPT_PATH))
    mod = importlib.import_module("backfill_sm_rejected_784")
    return importlib.reload(mod)


class FakeRunner:
    """Returns pre-canned responses for ``gh`` commands + records calls."""

    def __init__(self, responses: dict[str, str]):
        # Maps a substring match on the joined args to a response.
        self._responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        joined = " ".join(args)
        for key, value in self._responses.items():
            if key in joined:
                return value
        raise RuntimeError(f"FakeRunner: no response configured for {joined!r}")


def _rejected_payload(number: int, title: str = "x") -> dict:
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "sm:rejected"}, {"name": "art:code"}],
        "closedAt": "2026-10-30T12:00:00Z",
        "url": f"https://github.com/jcronq/alice/issues/{number}",
    }


def _pr_payload(
    pr_number: int, *, closes_issue: int | None, state: str = "MERGED"
) -> dict:
    refs = []
    if closes_issue is not None:
        refs.append({"number": closes_issue})
    return {
        "number": pr_number,
        "url": f"https://github.com/jcronq/alice/pull/{pr_number}",
        "state": state,
        "mergedAt": "2026-10-30T11:00:00Z" if state == "MERGED" else None,
        "closingIssuesReferences": refs,
    }


# ---------------------------------------------------------------------------
# list_rejected_issues
# ---------------------------------------------------------------------------


def test_list_rejected_issues_passes_search_query(backfill_module) -> None:
    runner = FakeRunner({"issue list": json.dumps([_rejected_payload(613)])})
    out = backfill_module.list_rejected_issues(
        "jcronq/alice",
        since=datetime(2026, 10, 1, tzinfo=timezone.utc),
        runner=runner,
    )
    assert len(out) == 1
    assert out[0]["number"] == 613
    args = runner.calls[0]
    assert "--state" in args
    assert "closed" in args
    search_idx = args.index("--search")
    assert "label:sm:rejected" in args[search_idx + 1]
    assert "closed:>=2026-10-01" in args[search_idx + 1]


def test_list_rejected_issues_handles_empty_stdout(backfill_module) -> None:
    runner = FakeRunner({"issue list": ""})
    out = backfill_module.list_rejected_issues(
        "jcronq/alice",
        since=datetime(2026, 10, 1, tzinfo=timezone.utc),
        runner=runner,
    )
    assert out == []


# ---------------------------------------------------------------------------
# find_merged_pr_for_issue
# ---------------------------------------------------------------------------


def test_find_merged_pr_for_issue_returns_match(backfill_module) -> None:
    runner = FakeRunner(
        {
            "pr list": json.dumps(
                [
                    _pr_payload(501, closes_issue=999),  # wrong issue
                    _pr_payload(502, closes_issue=613),  # match
                    _pr_payload(503, closes_issue=614),
                ]
            )
        }
    )
    pr = backfill_module.find_merged_pr_for_issue("jcronq/alice", 613, runner=runner)
    assert pr is not None
    assert pr["number"] == 502


def test_find_merged_pr_for_issue_returns_none_when_no_link(backfill_module) -> None:
    runner = FakeRunner({"pr list": json.dumps([_pr_payload(501, closes_issue=999)])})
    pr = backfill_module.find_merged_pr_for_issue("jcronq/alice", 613, runner=runner)
    assert pr is None


def test_find_merged_pr_skips_non_merged_state(backfill_module) -> None:
    runner = FakeRunner(
        {
            "pr list": json.dumps(
                [_pr_payload(502, closes_issue=613, state="CLOSED")]
            )
        }
    )
    pr = backfill_module.find_merged_pr_for_issue("jcronq/alice", 613, runner=runner)
    assert pr is None


# ---------------------------------------------------------------------------
# run_backfill end-to-end
# ---------------------------------------------------------------------------


def test_run_backfill_dry_run_does_not_edit(backfill_module) -> None:
    rejected = [_rejected_payload(613), _rejected_payload(614)]
    pr_for_613 = [_pr_payload(502, closes_issue=613)]
    pr_for_614 = []  # correctly rejected: no merged PR

    call_count = {"pr": 0}

    def runner(args: list[str]) -> str:
        joined = " ".join(args)
        if "issue list" in joined:
            return json.dumps(rejected)
        if "pr list" in joined:
            call_count["pr"] += 1
            if call_count["pr"] == 1:
                return json.dumps(pr_for_613)
            return json.dumps(pr_for_614)
        raise RuntimeError(f"unexpected call: {joined}")

    counters = backfill_module.run_backfill(
        "jcronq/alice",
        lookback_days=14,
        apply=False,
        runner=runner,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )
    assert counters == {
        "scanned": 2,
        "misrejected": 1,  # #613 would be re-labelled
        "relabelled": 0,  # dry-run
        "skipped": 0,
    }


def test_run_backfill_apply_relabels_misrejected(backfill_module) -> None:
    rejected = [_rejected_payload(613)]
    pr_match = [_pr_payload(502, closes_issue=613)]
    call_log: list[list[str]] = []

    def runner(args: list[str]) -> str:
        call_log.append(args)
        joined = " ".join(args)
        if "issue list" in joined:
            return json.dumps(rejected)
        if "pr list" in joined:
            return json.dumps(pr_match)
        if "issue edit" in joined:
            return ""
        if "issue comment" in joined:
            return ""
        raise RuntimeError(f"unexpected call: {joined}")

    counters = backfill_module.run_backfill(
        "jcronq/alice",
        lookback_days=14,
        apply=True,
        runner=runner,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )
    assert counters == {
        "scanned": 1,
        "misrejected": 1,
        "relabelled": 1,
        "skipped": 0,
    }
    # Verify the edit flipped labels and a comment was posted.
    edits = [c for c in call_log if "issue" in c and "edit" in c]
    assert len(edits) == 1
    assert "--add-label" in edits[0]
    assert "sm:done" in edits[0]
    assert "--remove-label" in edits[0]
    assert "sm:rejected" in edits[0]
    comments = [c for c in call_log if "issue" in c and "comment" in c]
    assert len(comments) == 1
    body_idx = comments[0].index("--body")
    assert "#784" in comments[0][body_idx + 1]
    assert "PR #502" in comments[0][body_idx + 1]


def test_run_backfill_skips_already_marked_sm_done(backfill_module) -> None:
    """Defensive: if an issue somehow carries both sm:rejected AND
    sm:done (manual fixup), skip it to avoid a redundant label write."""
    rejected = [
        {
            "number": 999,
            "title": "manually fixed",
            "labels": [{"name": "sm:rejected"}, {"name": "sm:done"}],
            "closedAt": "2026-10-30T12:00:00Z",
            "url": "https://github.com/jcronq/alice/issues/999",
        }
    ]

    def runner(args: list[str]) -> str:
        if "issue list" in " ".join(args):
            return json.dumps(rejected)
        raise RuntimeError("should not fetch PRs for sm:done-already issues")

    counters = backfill_module.run_backfill(
        "jcronq/alice",
        lookback_days=14,
        apply=True,
        runner=runner,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )
    assert counters["scanned"] == 1
    assert counters["skipped"] == 1
    assert counters["relabelled"] == 0
    assert counters["misrejected"] == 0


def test_run_backfill_handles_pr_relabel_failure_gracefully(backfill_module) -> None:
    """A failing ``issue edit`` bumps ``skipped`` but doesn't crash the
    sweep — the next issue still gets processed."""
    rejected = [_rejected_payload(613), _rejected_payload(614)]

    def runner(args: list[str]) -> str:
        joined = " ".join(args)
        if "issue list" in joined:
            return json.dumps(rejected)
        if "pr list" in joined:
            # Both issues have a matching merged PR.
            if "linked:issue" in joined:
                return json.dumps(
                    [
                        _pr_payload(501, closes_issue=613),
                        _pr_payload(502, closes_issue=614),
                    ]
                )
        if "issue edit" in joined and "613" in joined:
            raise RuntimeError("gh exploded on 613")
        if "issue edit" in joined:
            return ""
        if "issue comment" in joined:
            return ""
        raise RuntimeError(f"unexpected: {joined}")

    counters = backfill_module.run_backfill(
        "jcronq/alice",
        lookback_days=14,
        apply=True,
        runner=runner,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )
    assert counters["scanned"] == 2
    assert counters["misrejected"] == 2
    assert counters["relabelled"] == 1  # only 614 succeeded
    assert counters["skipped"] == 1  # 613 failed the edit
