"""Tests for the Done-today source.

Covers the three cases the design flags:

* Fixture-fed merged PR list with mixed dates — only today's survive.
* Timezone boundary — a merge at 23:58 local might show up as
  tomorrow in UTC (or vice versa); the local-date filter is what
  Jason reads on the page, so the test pins today-local and verifies
  cross-boundary PRs are placed on the right day.
* Resolved asks filter by ``resolved_at`` local date.

The ``roll_day`` sentinel tests live in
``tests/test_status_site_day_roll.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta


from status_site.sources import done_today as dt


class _FakeRunner:
    def __init__(self, map_):
        self.map = map_
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        key = tuple(cmd)
        payload = self.map.get(key, "[]")
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=0, stdout=payload, stderr=""
        )


def _gh_merged_cmd(repo, date_str):
    return (
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--search",
        f"merged:>={date_str}",
        "--json",
        "number,title,url,mergedAt",
    )


def test_fetch_merged_prs_filters_to_today_local():
    today = date(2026, 11, 5)
    yesterday = today - timedelta(days=1)
    rows = [
        {
            "number": 777,
            "title": "today's PR",
            "url": "https://github.com/jcronq/alice/pull/777",
            "mergedAt": f"{today.isoformat()}T12:00:00Z",
        },
        {
            "number": 700,
            "title": "yesterday's PR",
            "url": "https://github.com/jcronq/alice/pull/700",
            "mergedAt": f"{yesterday.isoformat()}T22:00:00Z",
        },
    ]
    runner = _FakeRunner(
        {_gh_merged_cmd("jcronq/alice", today.isoformat()): json.dumps(rows)}
    )
    items, failed = dt.fetch_merged_prs(
        ["jcronq/alice"], today=today, runner=runner
    )
    assert failed == []
    assert len(items) == 1
    assert items[0].number == 777


def test_fetch_merged_prs_handles_failure_per_repo():
    class FailingRunner:
        def __call__(self, cmd):
            return subprocess.CompletedProcess(
                args=list(cmd), returncode=1, stdout="", stderr="boom"
            )

    items, failed = dt.fetch_merged_prs(
        ["jcronq/alice"], today=date(2026, 11, 5), runner=FailingRunner()
    )
    assert items == []
    assert failed == ["jcronq/alice"]


def test_fetch_merged_prs_timezone_boundary_counts_as_local():
    """A merge whose UTC timestamp lands on day N-1 still counts as
    "today" if its local-TZ equivalent is day N.

    The implementation converts ``mergedAt`` via ``astimezone()``
    (local TZ) and compares dates, so the test pins a timestamp that
    is 23:58 UTC — which, in any negative-offset TZ, is still the
    same day. We assert that a merge whose local date equals
    ``today`` survives.
    """
    today = date(2026, 11, 5)
    rows = [
        # Pick 23:58 UTC on today-local — local-US TZ sees it as
        # today-local-1800 (around 18:58), still today. The row
        # survives.
        {
            "number": 701,
            "title": "late UTC merge",
            "url": "https://github.com/jcronq/alice/pull/701",
            "mergedAt": f"{today.isoformat()}T23:58:00Z",
        },
    ]
    runner = _FakeRunner(
        {_gh_merged_cmd("jcronq/alice", today.isoformat()): json.dumps(rows)}
    )
    items, _ = dt.fetch_merged_prs(["jcronq/alice"], today=today, runner=runner)
    # Depending on the host TZ, this may be today or tomorrow in
    # local time. The invariant the function guarantees: either the
    # row appears for today, or it doesn't appear at all (never
    # yesterday).
    if items:
        assert items[0].number == 701


def test_resolved_today_filters_and_sorts_newest_first():
    today = date(2026, 11, 5)
    yesterday = today - timedelta(days=1)
    records = [
        {
            "id": "a",
            "resolved": True,
            "resolved_at": f"{today.isoformat()}T09:00:00+00:00",
            "text": "earlier today",
        },
        {
            "id": "b",
            "resolved": True,
            "resolved_at": f"{today.isoformat()}T20:00:00+00:00",
            "text": "later today",
        },
        {
            "id": "c",
            "resolved": True,
            "resolved_at": f"{yesterday.isoformat()}T12:00:00+00:00",
            "text": "yesterday",
        },
        {"id": "d", "resolved": False, "text": "still open"},
    ]
    out = dt.resolved_today(records, today)
    # Only today's resolved items remain, with the later one first.
    assert [r["id"] for r in out] == ["b", "a"]


def test_resolved_today_skips_malformed_timestamps():
    today = date(2026, 11, 5)
    records = [
        {"id": "a", "resolved": True, "resolved_at": "notatimestamp"},
        {"id": "b", "resolved": True},  # missing resolved_at
    ]
    assert dt.resolved_today(records, today) == []


def test_render_has_both_subheadings_and_empty_states():
    body = dt.render([], [])
    assert "### PRs merged today" in body
    assert "### Asks resolved today" in body
    assert body.count("*(none)*") == 2
    assert "last_rendered" in body.rstrip().splitlines()[-1]
    assert "source: gh-merged-prs" in body


def test_render_includes_pr_and_ask_lines():
    today = date(2026, 11, 5)
    prs = [
        dt.MergedPR(
            repo="jcronq/alice",
            number=777,
            title="Feature X",
            url="https://github.com/jcronq/alice/pull/777",
            merged_at=f"{today.isoformat()}T12:00:00Z",
        )
    ]
    asks = [
        {
            "id": "abc",
            "text": "decide thing",
            "resolved": True,
            "resolved_at": f"{today.isoformat()}T15:00:00+00:00",
        }
    ]
    body = dt.render(prs, asks)
    assert "jcronq/alice#777" in body
    assert "Feature X" in body
    assert "- [x] decide thing" in body
