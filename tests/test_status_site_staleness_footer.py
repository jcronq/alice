"""Tests for the staleness footer contract.

Every polled-source renderer MUST append a trailing italic line of
the form ``*last_rendered: <iso> · source: <name>*``. The frontend
parser in ``static/index.html`` keys off this shape. Hand-edit zones
(known_issues) do NOT carry this footer.
"""

from __future__ import annotations

import re
from datetime import datetime

from status_site.sources import (
    done_today,
    known_issues,
    review_queue,
    running_now,
    waiting_on_you,
)


_FOOTER_RE = re.compile(
    r"^\*last_rendered:\s+(?P<ts>\S+)\s+·\s+source:\s+(?P<src>[a-z0-9-]+)\*$"
)


def _last_line(body: str) -> str:
    return body.rstrip().splitlines()[-1]


def test_running_now_footer_empty():
    assert _FOOTER_RE.match(_last_line(running_now.render([])))


def test_running_now_footer_nonempty():
    rows = [
        running_now.RunningTask(
            branch="feat/x",
            worktree="/tmp/x",
            alive=True,
            pr_url="",
            elapsed="1m",
            task_status="",
            issue="",
        )
    ]
    assert _FOOTER_RE.match(_last_line(running_now.render(rows)))


def test_review_queue_footer():
    assert _FOOTER_RE.match(_last_line(review_queue.render([], [])))


def test_waiting_on_you_footer(tmp_path):
    out = waiting_on_you.render(tmp_path / "missing.md", [])
    assert _FOOTER_RE.match(_last_line(out))


def test_done_today_footer():
    assert _FOOTER_RE.match(_last_line(done_today.render([], [])))


def test_known_issues_has_no_footer(tmp_path):
    out = known_issues.render(tmp_path / "missing.md")
    # Hand-edit zone — no staleness line.
    assert "last_rendered" not in out


def test_footer_timestamp_is_parseable_iso_with_offset():
    """The frontend uses ``Date.parse`` on the first whitespace token
    after ``last_rendered:``. The token must include a timezone offset
    (otherwise the browser's interpretation is implementation-defined
    and the staleness check becomes TZ-flaky)."""
    out = running_now.render([])
    m = _FOOTER_RE.match(_last_line(out))
    assert m is not None
    ts = m.group("ts")
    # datetime.fromisoformat (Python 3.11+) accepts the shape; if it
    # doesn't, Date.parse will be even flakier.
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
