"""Tests for the Done-today day-roll archive.

The hook writes yesterday's merged-PR + resolved-asks summary to
``done-archive.md`` and bumps a sentinel so a restart doesn't
double-append. Invariants:

* First call seeds the sentinel without rewinding history.
* Second call on the same day is a no-op (file unchanged).
* Next-day call writes a fresh section and bumps the sentinel.
* Missing status_dir is auto-created.
"""

from __future__ import annotations

from datetime import date, timedelta


from status_site.sources import done_today as dt


def test_first_call_seeds_sentinel_without_archiving(tmp_path):
    rolled = dt.roll_day(tmp_path, today=date(2026, 11, 5))
    assert rolled is True
    # No archive content yet because no loader provided any items.
    archive = tmp_path / "done-archive.md"
    assert not archive.exists()
    sentinel = tmp_path / dt.DAY_ROLL_SENTINEL_NAME
    assert sentinel.read_text().strip() == "2026-11-05"


def test_same_day_second_call_is_noop(tmp_path):
    dt.roll_day(tmp_path, today=date(2026, 11, 5))
    sentinel = tmp_path / dt.DAY_ROLL_SENTINEL_NAME
    mtime_before = sentinel.stat().st_mtime_ns
    assert dt.roll_day(tmp_path, today=date(2026, 11, 5)) is False
    assert sentinel.stat().st_mtime_ns == mtime_before


def test_next_day_rolls_yesterday_with_archive_write(tmp_path):
    today = date(2026, 11, 5)
    tomorrow = today + timedelta(days=1)

    pr = dt.MergedPR(
        repo="jcronq/alice",
        number=777,
        title="shipped yesterday",
        url="https://github.com/jcronq/alice/pull/777",
        merged_at="2026-11-05T12:00:00Z",
    )
    ask = {"id": "a", "text": "decided", "resolved": True,
           "resolved_at": "2026-11-05T15:00:00+00:00"}

    def _prs(d):
        return [pr] if d == today else []

    def _asks(d):
        return [ask] if d == today else []

    # Seed the sentinel at `today`.
    dt.roll_day(tmp_path, today=today)
    # Roll into tomorrow — last == today, so yesterday-in-the-sense-of
    # "the day being archived" is today; the function should append
    # that day's content.
    rolled = dt.roll_day(
        tmp_path, today=tomorrow, load_prs=_prs, load_asks=_asks
    )
    assert rolled is True
    archive = (tmp_path / "done-archive.md").read_text()
    assert "## 2026-11-05" in archive
    assert "jcronq/alice#777" in archive
    assert "- [x] decided" in archive
    sentinel = tmp_path / dt.DAY_ROLL_SENTINEL_NAME
    assert sentinel.read_text().strip() == tomorrow.isoformat()


def test_restart_safe_against_double_archive(tmp_path):
    """Simulate a restart: the sentinel survives on disk, so the next
    roll_day call for the same day is a no-op even after the server
    bounces. Exercises the on-disk sentinel contract, not just the
    in-process memoisation."""
    today = date(2026, 11, 5)
    tomorrow = today + timedelta(days=1)
    pr = dt.MergedPR(
        repo="jcronq/alice",
        number=777,
        title="x",
        url="https://github.com/jcronq/alice/pull/777",
        merged_at="2026-11-05T12:00:00Z",
    )
    dt.roll_day(tmp_path, today=today)
    dt.roll_day(tmp_path, today=tomorrow, load_prs=lambda d: [pr])
    archive1 = (tmp_path / "done-archive.md").read_text()
    # Second call for the SAME tomorrow — no new write.
    dt.roll_day(tmp_path, today=tomorrow, load_prs=lambda d: [pr])
    archive2 = (tmp_path / "done-archive.md").read_text()
    assert archive1 == archive2


def test_missing_dir_autocreated(tmp_path):
    target = tmp_path / "status" / "subdir"
    assert not target.exists()
    assert dt.roll_day(target, today=date(2026, 11, 5)) is True
    assert target.exists()
