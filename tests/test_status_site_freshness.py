"""Freshness tile classifier (#641)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from status_site import freshness

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "age_s,state",
    [(0, "ok"), (299, "ok"), (300, "amber"), (899, "amber"), (900, "red"), (86400, "red")],
)
def test_thresholds(age_s, state):
    src = NOW - timedelta(seconds=age_s)
    assert freshness.classify(src, now=NOW) == state


def test_unknown_when_source_missing_or_garbage():
    assert freshness.classify(None, now=NOW) == "unknown"
    assert freshness.classify("not a date", now=NOW) == "unknown"
    assert freshness.classify(True, now=NOW) == "unknown"


def test_future_timestamp_is_fresh():
    assert freshness.classify(NOW + timedelta(hours=1), now=NOW) == "ok"


def test_accepts_iso_epoch_and_naive():
    assert freshness.classify("2026-11-06T11:59:00Z", now=NOW) == "ok"
    assert freshness.classify(NOW.timestamp() - 600, now=NOW) == "amber"
    assert freshness.classify("2026-11-06T11:00:00", now=NOW) == "red"


def test_custom_thresholds():
    src = NOW - timedelta(seconds=30)
    assert freshness.classify(src, now=NOW, warn_after_seconds=10, bad_after_seconds=60) == "amber"


def test_tile_payload_and_render_lag():
    src = NOW - timedelta(seconds=400)
    t = freshness.tile("Pipeline", last_render=NOW - timedelta(seconds=100), last_source_mtime=src, now=NOW)
    assert t["state"] == "amber"
    assert t["age_seconds"] == 400
    assert t["render_lag_seconds"] == 300
    assert t["state"] in freshness.STATES


def test_tile_unknown_has_no_age():
    t = freshness.tile("X", now=NOW)
    assert t["state"] == "unknown"
    assert t["age_seconds"] is None and t["render_lag_seconds"] is None
