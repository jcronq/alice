"""Tests for :mod:`alice_thinking.memory_worker.thalamus`.

Split into two layers:

- **Rule-level tests** exercise :func:`route_events` directly with
  hand-built :class:`IntakeEvent` lists so we hit the decision logic
  without disk I/O. Fast, deterministic, no fixtures.
- **Filesystem-level tests** run :func:`run` against a tmp-path vault
  seeded with a handful of intake markdown files. Lighter coverage —
  just enough to confirm the scan → route → write → consume plumbing
  works end-to-end.

Thresholds live at module scope; tests reference them by name rather
than by magic number so a bump to :data:`MOTION_COALESCE_WINDOW_SECONDS`
etc. doesn't silently break the assertions.
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from alice_thinking.memory_worker import thalamus
from alice_thinking.memory_worker.thalamus import (
    BRIGHTNESS_MIN_DELTA,
    Decision,
    IntakeEvent,
    MOTION_COALESCE_WINDOW_SECONDS,
    ROOM_QUIET_THRESHOLD_SECONDS,
    TEMPERATURE_MIN_DELTA_F,
    route_events,
)


UTC = datetime.timezone.utc


def _ev(
    *,
    source: str = "cozylobe",
    kind: str,
    room: str | None = None,
    entity_id: str | None = None,
    value: str | None = None,
    value_num: float | None = None,
    observed_at: datetime.datetime,
    frontmatter: dict | None = None,
    body: str = "",
    path: pathlib.Path | None = None,
) -> IntakeEvent:
    """Convenience constructor. The pure route_events layer doesn't
    care about the file path — a bare Path stub is fine."""
    return IntakeEvent(
        path=path or pathlib.Path(f"/tmp/{kind}-{int(observed_at.timestamp())}.md"),
        source=source,
        kind=kind,
        room=room,
        entity_id=entity_id,
        value=value,
        value_num=value_num,
        observed_at=observed_at,
        frontmatter=frontmatter or {"source": source, "kind": kind},
        body=body,
    )


def _find(decisions: list[Decision], *, route: str, kind: str) -> list[Decision]:
    return [d for d in decisions if d.route == route and d.kind == kind]


# ---------- fast-path ----------


def test_fast_path_kinds_drop_with_audit_reason() -> None:
    """smoke/glass_break/doorbell → dropped/ with reason
    fast_path_already_handled. Applies regardless of source."""
    now = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(kind="smoke_detected", entity_id="binary_sensor.kitchen_smoke", observed_at=now),
        _ev(kind="glass_break", entity_id="binary_sensor.front_glass", observed_at=now),
        _ev(kind="doorbell_pressed", entity_id="button.front_door", observed_at=now),
    ]
    decisions = route_events(events, reference_time=now)
    dropped = [d for d in decisions if d.route == "dropped"]
    assert len(dropped) == 3
    assert all(d.reason == "fast_path_already_handled" for d in dropped)
    assert not [d for d in decisions if d.route == "filtered"]


# ---------- motion coalescing ----------


def test_motion_single_event_drops_as_no_sustained_activity() -> None:
    """One motion event in a room → not sustained → drop."""
    now = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(
            kind="motion",
            room="living_room",
            entity_id="binary_sensor.living_motion",
            value="on",
            observed_at=now,
        )
    ]
    decisions = route_events(events, reference_time=now)
    filtered = _find(decisions, route="filtered", kind="motion_coalesced")
    dropped = _find(decisions, route="dropped", kind="motion")
    assert filtered == []
    assert len(dropped) == 1
    assert dropped[0].reason == "no_sustained_activity"


def test_motion_two_events_within_window_coalesces() -> None:
    """Two events in the same room within the 5-min window → single
    coalesced observation with duration + event_count."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    t1 = t0 + datetime.timedelta(seconds=MOTION_COALESCE_WINDOW_SECONDS - 10)
    events = [
        _ev(kind="motion", room="living_room", value="on", observed_at=t0),
        _ev(kind="motion", room="living_room", value="off", observed_at=t1),
    ]
    decisions = route_events(events, reference_time=t1)
    filtered = _find(decisions, route="filtered", kind="motion_coalesced")
    dropped = _find(decisions, route="dropped", kind="motion")
    assert len(filtered) == 1
    assert dropped == []
    fm = filtered[0].frontmatter
    assert fm["event_count"] == 2
    assert fm["duration_seconds"] == MOTION_COALESCE_WINDOW_SECONDS - 10
    assert fm["room"] == "living_room"
    assert len(filtered[0].sources) == 2


def test_motion_events_beyond_window_split_into_separate_groups() -> None:
    """Events with a gap larger than the coalescing window are treated
    as two independent windows. Each window is evaluated on its own —
    the two events straddling a big gap don't coalesce together."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    # Two clusters of two events each, separated by 20 min > window.
    t1 = t0 + datetime.timedelta(seconds=60)
    t2 = t0 + datetime.timedelta(seconds=MOTION_COALESCE_WINDOW_SECONDS + 600)
    t3 = t2 + datetime.timedelta(seconds=60)
    events = [
        _ev(kind="motion", room="office", observed_at=t0),
        _ev(kind="motion", room="office", observed_at=t1),
        _ev(kind="motion", room="office", observed_at=t2),
        _ev(kind="motion", room="office", observed_at=t3),
    ]
    decisions = route_events(events, reference_time=t3)
    filtered = _find(decisions, route="filtered", kind="motion_coalesced")
    assert len(filtered) == 2
    for f in filtered:
        assert f.frontmatter["event_count"] == 2


def test_motion_room_quiet_emitted_when_last_motion_is_old() -> None:
    """A room whose most recent motion is older than the room-quiet
    threshold relative to reference_time → one room_quiet observation
    on top of any coalescing decision for that room."""
    now = datetime.datetime(2026, 9, 4, 18, 30, tzinfo=UTC)
    # Two events in the room, but the latest is 20 min in the past.
    last_motion = now - datetime.timedelta(seconds=ROOM_QUIET_THRESHOLD_SECONDS + 300)
    earlier = last_motion - datetime.timedelta(seconds=30)
    events = [
        _ev(kind="motion", room="master_bedroom", observed_at=earlier),
        _ev(kind="motion", room="master_bedroom", observed_at=last_motion),
    ]
    decisions = route_events(events, reference_time=now)
    quiet = _find(decisions, route="filtered", kind="room_quiet")
    assert len(quiet) == 1
    assert quiet[0].frontmatter["room"] == "master_bedroom"
    assert quiet[0].frontmatter["quiet_seconds"] >= ROOM_QUIET_THRESHOLD_SECONDS


def test_motion_room_quiet_not_emitted_when_room_still_active() -> None:
    """Room whose latest motion is within the threshold → no
    room_quiet observation."""
    now = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    t0 = now - datetime.timedelta(seconds=60)
    t1 = now - datetime.timedelta(seconds=30)
    events = [
        _ev(kind="motion", room="kitchen", observed_at=t0),
        _ev(kind="motion", room="kitchen", observed_at=t1),
    ]
    decisions = route_events(events, reference_time=now)
    assert _find(decisions, route="filtered", kind="room_quiet") == []


# ---------- temperature aggregation ----------


def test_temperature_below_threshold_drops_all_contributors() -> None:
    """Delta < 2°F → every contributing intake drops as
    below_threshold."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(
            kind="temperature",
            entity_id="sensor.living_temp",
            value="72.0",
            value_num=72.0,
            observed_at=t0,
        ),
        _ev(
            kind="temperature",
            entity_id="sensor.living_temp",
            value="73.5",
            value_num=73.5,
            observed_at=t0 + datetime.timedelta(seconds=90),
        ),
    ]
    decisions = route_events(events, reference_time=t0)
    filtered = _find(decisions, route="filtered", kind="state_change")
    dropped = _find(decisions, route="dropped", kind="temperature")
    assert filtered == []
    assert len(dropped) == 2
    assert all(d.reason == "below_threshold" for d in dropped)
    # Sanity: the observed delta made it into the dropped record.
    assert dropped[0].frontmatter["delta_threshold"] == TEMPERATURE_MIN_DELTA_F


def test_temperature_at_or_above_threshold_coalesces_to_state_change() -> None:
    """Delta ≥ 2°F → one state_change observation. sub_kind identifies
    the source kind so downstream can distinguish temp vs light."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(
            kind="temperature",
            entity_id="sensor.living_temp",
            room="living_room",
            value_num=68.0,
            observed_at=t0,
        ),
        _ev(
            kind="temperature",
            entity_id="sensor.living_temp",
            room="living_room",
            value_num=70.0,  # exactly threshold
            observed_at=t0 + datetime.timedelta(seconds=120),
        ),
    ]
    decisions = route_events(events, reference_time=t0)
    filtered = _find(decisions, route="filtered", kind="state_change")
    assert len(filtered) == 1
    fm = filtered[0].frontmatter
    assert fm["sub_kind"] == "temperature"
    assert fm["delta"] == 2.0
    assert fm["delta_units"] == "F"
    assert fm["start_value"] == 68.0
    assert fm["end_value"] == 70.0
    assert fm["event_count"] == 2


def test_temperature_events_per_entity_are_independent() -> None:
    """Two entities in the same room, one below threshold, one above
    → one drops, one coalesces. Aggregation is per-entity, not
    per-room, so a quiet sensor doesn't get pulled along by a noisy
    one."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(kind="temperature", entity_id="sensor.a", value_num=70.0, observed_at=t0),
        _ev(kind="temperature", entity_id="sensor.a", value_num=71.0, observed_at=t0),
        _ev(kind="temperature", entity_id="sensor.b", value_num=70.0, observed_at=t0),
        _ev(kind="temperature", entity_id="sensor.b", value_num=75.0, observed_at=t0),
    ]
    decisions = route_events(events, reference_time=t0)
    filtered = _find(decisions, route="filtered", kind="state_change")
    dropped = _find(decisions, route="dropped", kind="temperature")
    assert len(filtered) == 1
    assert filtered[0].frontmatter["entity_id"] == "sensor.b"
    assert len(dropped) == 2
    assert all(d.frontmatter["entity_id"] == "sensor.a" for d in dropped)


# ---------- light_change aggregation ----------


def test_light_change_below_5_percent_drops() -> None:
    """Brightness delta < 5% → drop as below_threshold. Uses the
    dedicated BRIGHTNESS_MIN_DELTA threshold, not the temperature one."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(
            kind="light_change",
            entity_id="light.living_1",
            value_num=0.80,
            observed_at=t0,
        ),
        _ev(
            kind="light_change",
            entity_id="light.living_1",
            value_num=0.83,  # +3%, below threshold
            observed_at=t0 + datetime.timedelta(seconds=30),
        ),
    ]
    decisions = route_events(events, reference_time=t0)
    filtered = _find(decisions, route="filtered", kind="state_change")
    dropped = _find(decisions, route="dropped", kind="light_change")
    assert filtered == []
    assert len(dropped) == 2
    assert dropped[0].frontmatter["delta_threshold"] == BRIGHTNESS_MIN_DELTA


def test_light_change_at_or_above_5_percent_coalesces() -> None:
    """Brightness delta ≥ 5% → coalesced state_change with sub_kind
    light_change."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(kind="light_change", entity_id="light.a", value_num=0.20, observed_at=t0),
        _ev(
            kind="light_change",
            entity_id="light.a",
            value_num=0.25,  # exactly threshold
            observed_at=t0 + datetime.timedelta(seconds=45),
        ),
    ]
    decisions = route_events(events, reference_time=t0)
    filtered = _find(decisions, route="filtered", kind="state_change")
    assert len(filtered) == 1
    fm = filtered[0].frontmatter
    assert fm["sub_kind"] == "light_change"
    assert fm["delta"] == pytest.approx(0.05, abs=1e-9)


# ---------- passthrough + unclassified ----------


def test_passthrough_kinds_survive_untouched() -> None:
    """thinking surface/observation + speaking note/surface pass
    through unchanged, one filtered decision per event, thalamus_route
    marker in frontmatter for downstream promoters."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(source="thinking", kind="surface", observed_at=t0, body="thinker surface"),
        _ev(source="thinking", kind="observation", observed_at=t0, body="observation body"),
        _ev(source="speaking", kind="note", observed_at=t0, body="speaking note"),
    ]
    decisions = route_events(events, reference_time=t0)
    filtered = [d for d in decisions if d.route == "filtered"]
    assert len(filtered) == 3
    assert all(d.frontmatter.get("thalamus_route") == "passthrough" for d in filtered)


def test_unclassified_cozylobe_kind_drops_with_no_matching_rule() -> None:
    """A cozylobe kind not covered by the routing table (e.g. a new
    sensor type) drops explicitly rather than silently falling
    through — better than the old noise/ shelf."""
    t0 = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    events = [
        _ev(
            source="cozylobe",
            kind="humidity",  # not in the routing table
            entity_id="sensor.living_humidity",
            observed_at=t0,
        )
    ]
    decisions = route_events(events, reference_time=t0)
    dropped = [d for d in decisions if d.route == "dropped"]
    assert len(dropped) == 1
    assert dropped[0].reason == "no_matching_rule"


# ---------- filesystem integration ----------


@pytest.fixture
def thalamus_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp-path vault with just the thalamus dirs the module expects."""
    root = tmp_path / "inner" / "thalamus"
    (root / "intake").mkdir(parents=True)
    (root / "filtered").mkdir()
    (root / "dropped").mkdir()
    (root / ".consumed").mkdir()
    return tmp_path


def _drop_intake(vault: pathlib.Path, name: str, content: str) -> pathlib.Path:
    p = vault / "inner" / "thalamus" / "intake" / name
    p.write_text(content, encoding="utf-8")
    return p


def test_run_on_empty_intake_is_noop(thalamus_vault: pathlib.Path) -> None:
    """Empty intake → zeroed report, no files created anywhere."""
    report = thalamus.run(thalamus_vault)
    assert report.scanned == 0
    assert report.filtered == 0
    assert report.dropped == 0
    filtered_dir = thalamus_vault / "inner" / "thalamus" / "filtered"
    dropped_dir = thalamus_vault / "inner" / "thalamus" / "dropped"
    assert list(filtered_dir.iterdir()) == []
    assert list(dropped_dir.iterdir()) == []


def test_run_drops_fast_path_intake_and_consumes(thalamus_vault: pathlib.Path) -> None:
    """A fast-path intake file → dropped/ output, source moved to
    .consumed/<today>/."""
    now = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    epoch = int(now.timestamp())
    name = f"{epoch}-cozylobe-doorbell_pressed.md"
    _drop_intake(
        thalamus_vault,
        name,
        (
            "---\n"
            "source: cozylobe\n"
            "kind: doorbell_pressed\n"
            "entity_id: button.front_door\n"
            f"observed_at: {now.isoformat()}\n"
            "---\n\n"
            "Doorbell pressed.\n"
        ),
    )
    report = thalamus.run(thalamus_vault, reference_time=now)
    assert report.scanned == 1
    assert report.dropped == 1
    assert report.filtered == 0
    dropped_dir = thalamus_vault / "inner" / "thalamus" / "dropped"
    assert any(p.name == name for p in dropped_dir.iterdir())
    # Intake gone; consumed archive holds it.
    intake = thalamus_vault / "inner" / "thalamus" / "intake" / name
    assert not intake.exists()
    consumed = (
        thalamus_vault
        / "inner"
        / "thalamus"
        / ".consumed"
        / datetime.date.today().isoformat()
        / name
    )
    assert consumed.exists()


def test_run_coalesces_multiple_motion_intakes_into_one_filtered(
    thalamus_vault: pathlib.Path,
) -> None:
    """Three motion intakes in the same room within the window
    produce one filtered coalesced observation and consume all three
    intake files."""
    base = datetime.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)
    for i in range(3):
        ts = base + datetime.timedelta(seconds=i * 60)
        _drop_intake(
            thalamus_vault,
            f"{int(ts.timestamp())}-cozylobe-motion-{i}.md",
            (
                "---\n"
                "source: cozylobe\n"
                "kind: motion\n"
                "room: living_room\n"
                "entity_id: binary_sensor.living_motion\n"
                f"observed_at: {ts.isoformat()}\n"
                "value: on\n"
                "---\n\nMotion.\n"
            ),
        )
    report = thalamus.run(thalamus_vault, reference_time=base + datetime.timedelta(seconds=180))
    assert report.scanned == 3
    assert report.filtered == 1
    assert report.coalesced == 1
    assert report.dropped == 0
    intake_dir = thalamus_vault / "inner" / "thalamus" / "intake"
    assert [p for p in intake_dir.iterdir() if p.name.endswith(".md")] == []
    filtered_dir = thalamus_vault / "inner" / "thalamus" / "filtered"
    filtered_files = list(filtered_dir.iterdir())
    assert len(filtered_files) == 1
    text = filtered_files[0].read_text(encoding="utf-8")
    assert "motion_coalesced" in text
    assert "event_count: 3" in text
