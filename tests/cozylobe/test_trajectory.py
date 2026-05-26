"""Tests for the cozylobe trajectory recorder (Phase 4 of #381)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alice_cozylobe.trajectories import (
    TIME_BUCKETS,
    TRAJECTORY_PROMOTION_CURVE,
    bucket_for_hour,
    iter_trajectories,
    load_trajectory,
    record_trajectory,
    trajectory_path,
    trajectory_weight,
)


# ---------------------------------------------------------------------------
# Pure helpers


def test_bucket_for_hour_partitions_day():
    """Every 0..23 hour must map to exactly one bucket."""
    seen: dict[int, str] = {}
    for hour in range(24):
        seen[hour] = bucket_for_hour(hour)
    # All buckets are valid.
    for bucket in seen.values():
        assert bucket in TIME_BUCKETS
    # Specific spot checks per the design's bucket scheme.
    assert bucket_for_hour(0) == "00-06"
    assert bucket_for_hour(5) == "00-06"
    assert bucket_for_hour(6) == "06-12"
    assert bucket_for_hour(11) == "06-12"
    assert bucket_for_hour(12) == "12-18"
    assert bucket_for_hour(17) == "12-18"
    assert bucket_for_hour(18) == "18-24"
    assert bucket_for_hour(23) == "18-24"


def test_trajectory_weight_curve_is_monotone():
    counts = [c for c, _ in TRAJECTORY_PROMOTION_CURVE]
    weights = [w for _, w in TRAJECTORY_PROMOTION_CURVE]
    assert counts == sorted(counts)
    assert weights == sorted(weights)


def test_trajectory_weight_caps_at_curve_max():
    """Once observation_count exceeds the largest curve threshold, the
    weight stays at the curve's max value (the asymptote)."""
    last_count, last_weight = TRAJECTORY_PROMOTION_CURVE[-1]
    assert trajectory_weight(last_count) == pytest.approx(last_weight)
    assert trajectory_weight(last_count * 10) == pytest.approx(last_weight)


def test_trajectory_weight_returns_zero_below_first_threshold():
    first_count, first_weight = TRAJECTORY_PROMOTION_CURVE[0]
    if first_count > 0:
        assert trajectory_weight(first_count - 1) == 0.0
    assert trajectory_weight(first_count) == pytest.approx(first_weight)


# ---------------------------------------------------------------------------
# trajectory_path


def test_trajectory_path_includes_person_subdir(tmp_path: Path):
    """Trajectories are organized as
    ``trajectories/<person>/<from>-to-<to>.md``."""
    path = trajectory_path(tmp_path, "Jason", "Bedroom", "Kitchen")
    rel = path.relative_to(tmp_path)
    parts = rel.parts
    assert parts[0] == "trajectories"
    assert parts[1] == "jason"
    assert parts[2] == "bedroom-to-kitchen.md"


def test_trajectory_path_handles_unknown_person(tmp_path: Path):
    path = trajectory_path(tmp_path, None, "Kitchen", "Office")
    rel = path.relative_to(tmp_path)
    assert rel.parts[1] == "unknown"


# ---------------------------------------------------------------------------
# record_trajectory


def test_record_trajectory_creates_note(tmp_path: Path):
    ts = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    record = record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts)
    assert record.path is not None
    assert record.path.is_file()
    assert record.observation_count == 1
    assert record.time_buckets == {"06-12"}
    assert record.confidence == pytest.approx(trajectory_weight(1))
    # Reload from disk and confirm the round-trip.
    loaded = load_trajectory(record.path)
    assert loaded is not None
    assert loaded.person == "Jason"
    assert loaded.from_room == "Bedroom"
    assert loaded.to_room == "Kitchen"
    assert loaded.observation_count == 1
    assert loaded.time_buckets == {"06-12"}


def test_record_trajectory_increments_existing_count(tmp_path: Path):
    """Recording the same edge twice should bump observation_count to 2
    and recompute confidence per the curve."""
    ts1 = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 26, 7, 45, tzinfo=timezone.utc)
    record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts1)
    record = record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts2)
    assert record.observation_count == 2
    assert record.confidence == pytest.approx(trajectory_weight(2))


def test_record_trajectory_accumulates_time_buckets(tmp_path: Path):
    """Same edge at different times-of-day → both buckets present."""
    morning = datetime(2026, 5, 26, 7, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 5, 26, 22, 0, tzinfo=timezone.utc)
    record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", morning)
    record = record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", evening)
    assert record.time_buckets == {"06-12", "18-24"}


def test_record_trajectory_separate_persons_get_separate_files(tmp_path: Path):
    ts = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    rec_jason = record_trajectory(tmp_path, "Jason", "Kitchen", "Office", ts)
    rec_katie = record_trajectory(tmp_path, "Katie", "Kitchen", "Office", ts)
    assert rec_jason.path != rec_katie.path
    assert rec_jason.path.parent.name == "jason"
    assert rec_katie.path.parent.name == "katie"
    # Each has its own count.
    assert rec_jason.observation_count == 1
    assert rec_katie.observation_count == 1


def test_record_trajectory_rejects_missing_rooms(tmp_path: Path):
    ts = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        record_trajectory(tmp_path, "Jason", "", "Kitchen", ts)
    with pytest.raises(ValueError):
        record_trajectory(tmp_path, "Jason", "Kitchen", "", ts)


def test_record_trajectory_writes_atomic(tmp_path: Path):
    """No .tmp- siblings should remain after a successful write."""
    ts = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts)
    leftovers = list((tmp_path / "trajectories" / "jason").glob(".tmp-*"))
    assert leftovers == []


def test_record_trajectory_inline_edge_in_body(tmp_path: Path):
    """The body must carry an inline ``(OFTEN-VISITS:<weight>)[[rooms/<to>]]``
    edge so the relationship is queryable through the cortex graph."""
    ts = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    record = record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts)
    text = record.path.read_text()
    assert "(OFTEN-VISITS:" in text
    assert "[[rooms/Kitchen]]" in text


def test_record_trajectory_naive_datetime_treated_as_utc(tmp_path: Path):
    """Passing a naive datetime shouldn't crash; it gets treated as UTC."""
    ts = datetime(2026, 5, 26, 7, 30)  # naive
    record = record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts)
    assert record.time_buckets == {"06-12"}


def test_iter_trajectories_walks_all_records(tmp_path: Path):
    ts = datetime(2026, 5, 26, 7, 30, tzinfo=timezone.utc)
    record_trajectory(tmp_path, "Jason", "Bedroom", "Kitchen", ts)
    record_trajectory(tmp_path, "Jason", "Kitchen", "Office", ts)
    record_trajectory(tmp_path, "Katie", "Bedroom", "Kitchen", ts)

    records = list(iter_trajectories(tmp_path))
    assert len(records) == 3
    persons = {r.person for r in records}
    assert persons == {"Jason", "Katie"}


def test_iter_trajectories_empty_root(tmp_path: Path):
    assert list(iter_trajectories(tmp_path)) == []
