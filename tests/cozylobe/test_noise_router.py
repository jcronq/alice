"""Tests for the noise-vs-notes routing introduced in issue #411.

Three layers:

* :mod:`alice_cozylobe.noise_router` — entity-type classifier + burst
  coalescer.
* :mod:`alice_cozylobe.surfaces` — :func:`write_noise_note` path.
* :mod:`alice_cozylobe.motion` + :mod:`alice_cozylobe.wake_loop` —
  end-to-end routing through the motion pipeline and the backstop
  path.

The acceptance criteria from the dispatcher prompt map onto these
tests:

* "Routing: motion event → notes/noise/, incident event → notes/"
  → :func:`test_motion_pipeline_writes_to_noise_route` and
  :func:`test_security_motion_stays_on_notes_route`.
* "Coalesce: 3 motion events within 60s → one note with 3 entries,
  not three separate notes" → :func:`test_burst_coalescer_flushes_at_threshold`.
* "Coalesce timeout: a single motion event followed by 70s silence
  emits its own note (timeout, no further events to bundle with)"
  → :func:`test_burst_coalescer_flush_stale_emits_single_event_note`.
* "Routing-rule edge cases per the design table" → the
  ``test_should_route_*`` battery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from alice_cozylobe.events import CozyHemEvent
from alice_cozylobe.motion import MotionPipeline
from alice_cozylobe.noise_router import (
    BurstCoalescer,
    NoiseEvent,
    classify_entity_type,
    coalesce_slug,
    render_coalesced_body,
    should_route_to_noise,
)
from alice_cozylobe.qwen_client import QwenUnreachable
from alice_cozylobe.surfaces import write_noise_note
from alice_cozylobe.wake_loop import WakeLoop
from core.events import CapturingEmitter


# ---------------------------------------------------------------------------
# Helpers


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubQwen:
    """Stub QwenClient that returns a canned classification."""

    def __init__(self, payload: Optional[dict] = None) -> None:
        self.payload = payload or {
            "current_room": "Kitchen",
            "confidence": 0.6,
            "person_hypothesis": "Jason",
            "person_confidence": 0.5,
            "next_room_hypothesis": "Office",
            "next_room_confidence": 0.4,
            "reasoning": "Stub.",
        }

    async def complete(self, prompt: str) -> dict:
        return self.payload


def _motion_event(
    *,
    entity_id: str = "binary_sensor.hue_kitchen_motion",
    timestamp: float = 1_000.0,
) -> CozyHemEvent:
    return CozyHemEvent(
        kind="entity:update",
        entity_id=entity_id,
        payload={"state": "on"},
        received_at=timestamp,
    )


# ---------------------------------------------------------------------------
# Routing decision — should_route_to_noise / classify_entity_type


@pytest.mark.parametrize(
    "entity_id, expected_label",
    [
        # Motion sensors → noise / "motion"
        ("binary_sensor.hue_kitchen_motion", "motion"),
        ("binary_sensor.hue_master_closet_motion_sensor_motion", "motion"),
        ("binary_sensor.living_room_motion", "motion"),
        # Light level / illuminance → noise / "light_level"
        ("sensor.hue_kitchen_light_level", "light_level"),
        ("sensor.hue_office_lightlevel", "light_level"),
        ("sensor.hue_bedroom_illuminance", "light_level"),
        # Ambient telemetry → noise / "ambient"
        ("sensor.hue_kitchen_ambient_temp", "ambient"),
        ("sensor.hue_office_ambient_temperature", "ambient"),
        ("sensor.hue_bedroom_temperature", "ambient"),
        ("sensor.hue_bathroom_humidity", "ambient"),
    ],
)
def test_should_route_to_noise_for_low_value_telemetry(
    entity_id: str, expected_label: str
):
    """Every entity type the design table marks "noise" routes correctly."""
    assert should_route_to_noise(entity_id) is True
    assert classify_entity_type(entity_id) == expected_label


@pytest.mark.parametrize(
    "entity_id",
    [
        # Light state transitions — high-signal
        "light.living_room",
        "light.kitchen_overhead",
        # Switches and input booleans
        "switch.theater_power",
        "input_boolean.gym_lights_toggle",
        # Locks
        "lock.front_door",
        # Door / window contact sensors — security-relevant
        "binary_sensor.front_door",
        "binary_sensor.kitchen_window",
        # Scenes / buttons — explicit user actions
        "scene.bedtime",
        "button.kitchen_dimmer_press",
        # Unknown / empty
        "",
        "media_player.theater",
        "automation.daily_sync",
    ],
)
def test_should_route_to_noise_returns_false_for_signal_events(entity_id: str):
    """Non-noise entity types fall back to notes/ — the inbox path.

    Covers the rest of the design table's "notes/" column plus a few
    novel-shape entity_ids that the router doesn't recognize. Fail-
    open posture: unknown means notes/, not noise/.
    """
    assert should_route_to_noise(entity_id) is False
    assert classify_entity_type(entity_id) is None


# ---------------------------------------------------------------------------
# BurstCoalescer — threshold, window, stale flush


def test_burst_coalescer_buffers_below_threshold():
    """Two motion events should buffer without emitting a flush."""
    clock = _FakeClock()
    c = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    flush1 = c.add(
        NoiseEvent(
            timestamp=clock(),
            entity_id="binary_sensor.hue_kitchen_motion",
            entity_type="motion",
        )
    )
    clock.advance(1.0)
    flush2 = c.add(
        NoiseEvent(
            timestamp=clock(),
            entity_id="binary_sensor.hue_kitchen_motion",
            entity_type="motion",
        )
    )
    assert flush1 is None
    assert flush2 is None
    assert c.pending_count("motion") == 2


def test_burst_coalescer_flushes_at_threshold():
    """3 motion events within 60s → ONE coalesced flush with 3 entries.

    This is the central spec from the design: ``≥3 events of same
    entity type within 60s window → single coalesced note``.
    """
    clock = _FakeClock()
    c = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    c.add(NoiseEvent(timestamp=clock(), entity_id="m1", entity_type="motion"))
    clock.advance(10.0)
    c.add(NoiseEvent(timestamp=clock(), entity_id="m2", entity_type="motion"))
    clock.advance(10.0)
    flush = c.add(
        NoiseEvent(timestamp=clock(), entity_id="m3", entity_type="motion")
    )
    assert flush is not None
    assert flush.coalesced is True
    assert flush.entity_type == "motion"
    assert len(flush.events) == 3
    assert [e.entity_id for e in flush.events] == ["m1", "m2", "m3"]
    # Buffer reset after coalesce.
    assert c.pending_count("motion") == 0


def test_burst_coalescer_window_excludes_stale_events():
    """An event 70s after the first should NOT count it toward the
    threshold — the 60s window pruned it out."""
    clock = _FakeClock()
    c = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    c.add(NoiseEvent(timestamp=clock(), entity_id="m1", entity_type="motion"))
    clock.advance(70.0)  # past window
    # Two MORE events in quick succession — only these two should be
    # considered for the threshold; m1 was pruned.
    c.add(NoiseEvent(timestamp=clock(), entity_id="m2", entity_type="motion"))
    flush = c.add(
        NoiseEvent(timestamp=clock(), entity_id="m3", entity_type="motion")
    )
    # m1 was pruned out before m2 was added, so buffer holds [m2, m3].
    # That's only 2 events; threshold (3) not reached.
    assert flush is None
    assert c.pending_count("motion") == 2


def test_burst_coalescer_per_entity_type_buffers_are_independent():
    """A motion burst should not coalesce with light_level events."""
    clock = _FakeClock()
    c = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    # 2 motion + 2 light_level → neither reaches threshold of 3.
    c.add(NoiseEvent(timestamp=clock(), entity_id="m1", entity_type="motion"))
    c.add(
        NoiseEvent(
            timestamp=clock(),
            entity_id="l1",
            entity_type="light_level",
        )
    )
    c.add(NoiseEvent(timestamp=clock(), entity_id="m2", entity_type="motion"))
    flush = c.add(
        NoiseEvent(
            timestamp=clock(),
            entity_id="l2",
            entity_type="light_level",
        )
    )
    assert flush is None
    assert c.pending_count("motion") == 2
    assert c.pending_count("light_level") == 2


def test_burst_coalescer_flush_stale_emits_single_event_note():
    """A single noise event followed by 70s silence emits a stale
    flush — the timeout case from the dispatcher prompt.

    The flush carries the single event's data (no coalesced
    timestamp range needed) and is tagged as a stale-flush so
    downstream renders can distinguish from a true burst-coalesce.
    """
    clock = _FakeClock()
    c = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    c.add(NoiseEvent(timestamp=clock(), entity_id="l1", entity_type="light_level"))
    # No further events — but flush_stale before the window elapses
    # should be a no-op.
    assert c.flush_stale() == []
    clock.advance(70.0)
    flushes = c.flush_stale()
    assert len(flushes) == 1
    flush = flushes[0]
    assert flush.coalesced is False
    assert len(flush.events) == 1
    assert flush.events[0].entity_id == "l1"
    # Buffer was drained.
    assert c.pending_count("light_level") == 0


def test_burst_coalescer_flush_stale_multiple_types():
    """flush_stale should drain every stale buffer in one call."""
    clock = _FakeClock()
    c = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    c.add(NoiseEvent(timestamp=clock(), entity_id="m1", entity_type="motion"))
    c.add(NoiseEvent(timestamp=clock(), entity_id="l1", entity_type="light_level"))
    clock.advance(70.0)
    flushes = c.flush_stale()
    types = sorted(f.entity_type for f in flushes)
    assert types == ["light_level", "motion"]


def test_burst_coalescer_rejects_invalid_params():
    with pytest.raises(ValueError):
        BurstCoalescer(window_s=0)
    with pytest.raises(ValueError):
        BurstCoalescer(window_s=-1)
    with pytest.raises(ValueError):
        BurstCoalescer(threshold=0)


def test_render_coalesced_body_lists_every_event():
    """The coalesced body must include every originating event with
    its timestamp — data consolidated, not lost."""
    events = [
        NoiseEvent(timestamp=1_000.0, entity_id="m1", entity_type="motion", summary="state=on"),
        NoiseEvent(timestamp=1_010.0, entity_id="m2", entity_type="motion", summary="state=on"),
        NoiseEvent(timestamp=1_020.0, entity_id="m3", entity_type="motion", summary="state=off"),
    ]
    from alice_cozylobe.noise_router import CoalesceFlush

    flush = CoalesceFlush(
        events=events,
        entity_type="motion",
        coalesced=True,
        window_start=events[0].timestamp,
        window_end=events[-1].timestamp,
    )
    body = render_coalesced_body(flush)
    assert "m1" in body and "m2" in body and "m3" in body
    assert "state=on" in body
    assert "state=off" in body
    # The header notes the type and count.
    assert "motion" in body
    assert "3" in body


def test_coalesce_slug_is_filesystem_safe():
    from alice_cozylobe.noise_router import CoalesceFlush

    flush = CoalesceFlush(
        events=[
            NoiseEvent(timestamp=1.0, entity_id="x", entity_type="motion"),
            NoiseEvent(timestamp=2.0, entity_id="y", entity_type="motion"),
        ],
        entity_type="motion",
        coalesced=True,
    )
    slug = coalesce_slug(flush)
    assert slug
    assert "/" not in slug
    assert " " not in slug


# ---------------------------------------------------------------------------
# surfaces.write_noise_note — filesystem path


def test_write_noise_note_writes_into_noise_subdirectory(tmp_path: Path):
    """write_noise_note must land in inner/notes/noise/, NOT
    inner/notes/. Thinking's inbox-drain uses non-recursive
    iterdir/glob — the subdirectory is invisible to it."""
    path = write_noise_note(
        "Sample noise body.",
        slug="motion-burst-x3",
        tags=("lobe-observation", "noise"),
        mind=tmp_path,
    )
    assert (tmp_path / "inner" / "notes" / "noise").is_dir()
    assert path.parent == tmp_path / "inner" / "notes" / "noise"
    # Sanity: nothing landed in the inbox path.
    assert not list((tmp_path / "inner" / "notes").glob("*.md"))


def test_write_noise_note_includes_route_marker(tmp_path: Path):
    """The frontmatter should carry ``route: noise`` so a vault audit
    can identify noise-routed writes without walking the filesystem."""
    path = write_noise_note(
        "body",
        slug="x",
        mind=tmp_path,
    )
    content = path.read_text()
    assert "route: noise" in content
    assert "source: cozylobe" in content


def test_write_noise_note_idempotent_directory_creation(tmp_path: Path):
    """First write creates inner/notes/noise/; second write reuses it."""
    write_noise_note("a", slug="x1", mind=tmp_path)
    write_noise_note("b", slug="x2", mind=tmp_path)
    paths = list((tmp_path / "inner" / "notes" / "noise").glob("*.md"))
    assert len(paths) == 2


# ---------------------------------------------------------------------------
# MotionPipeline routing


@pytest.mark.asyncio
async def test_motion_pipeline_writes_to_noise_route():
    """A normal (non-security) motion batch should write via the
    noise writer, NOT the notes writer.

    This is the dispatcher's "motion event → notes/noise/" assertion.
    """
    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    pipeline = MotionPipeline(
        qwen_client=_StubQwen(),
        vault=None,
        write_note=_notes,
        write_noise=_noise,
        # Non-security so the noise route fires.
        security_predicate=lambda _e: False,
    )
    # Flush by forcing batch size: configure a max_batch_size of 1 by
    # using the default queue + advancing the security flag is False.
    # Easier: call _classify_and_write directly with security=False.
    from alice_cozylobe.motion import MotionEvent

    motion = MotionEvent(
        timestamp=1_000.0,
        entity_id="binary_sensor.hue_kitchen_motion",
        state="on",
        room_id="Kitchen",
    )
    await pipeline._classify_and_write([motion], security=False)
    assert len(noise_writes) == 1
    assert notes_writes == []
    # Tag carries the motion-pipeline marker so thinking can spot it
    # even if it ever audits noise/.
    assert "motion-pipeline" in noise_writes[0]["tags"]


@pytest.mark.asyncio
async def test_security_motion_stays_on_notes_route():
    """Security-class motion (nighttime window) MUST route to notes/
    so thinking sees it on the next wake. This is the
    "incident event → notes/" half of the routing contract.
    """
    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    pipeline = MotionPipeline(
        qwen_client=_StubQwen(),
        vault=None,
        write_note=_notes,
        write_noise=_noise,
        security_predicate=lambda _e: True,
    )
    from alice_cozylobe.motion import MotionEvent

    motion = MotionEvent(
        timestamp=1_000.0,
        entity_id="binary_sensor.hue_kitchen_motion",
        state="on",
        room_id="Kitchen",
    )
    await pipeline._classify_and_write([motion], security=True)
    assert len(notes_writes) == 1
    assert noise_writes == []
    assert "motion-security" in notes_writes[0]["tags"]


@pytest.mark.asyncio
async def test_degraded_motion_note_routes_to_notes_during_outage():
    """Classifier-outage escalation: when qwen is unreachable the
    motion-degraded note routes to inner/notes/ so thinking can
    review patterns post-outage (the design's "all events surface to
    notes/ during outage" rule).
    """

    class _BrokenQwen:
        async def complete(self, prompt: str) -> dict:
            raise QwenUnreachable("desktop offline")

    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    pipeline = MotionPipeline(
        qwen_client=_BrokenQwen(),
        write_note=_notes,
        write_noise=_noise,
        security_predicate=lambda _e: False,
    )
    from alice_cozylobe.motion import MotionEvent

    motion = MotionEvent(
        timestamp=1_000.0,
        entity_id="binary_sensor.hue_kitchen_motion",
        state="on",
        room_id="Kitchen",
    )
    await pipeline._classify_and_write([motion], security=False)
    # Degraded path writes to notes/, not noise/.
    assert len(notes_writes) == 1
    assert noise_writes == []
    assert "motion-degraded" in notes_writes[0]["tags"]


# ---------------------------------------------------------------------------
# WakeLoop backstop routing — non-motion noise + signal


@pytest.mark.asyncio
async def test_wake_loop_backstop_routes_light_level_to_noise():
    """A backstop note for a sensor.*_light_level event should be
    buffered in the noise coalescer; first event below threshold
    yields no immediate write."""
    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    clock = _FakeClock()
    coalescer = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    emitter = CapturingEmitter()
    wake = WakeLoop(
        emitter=emitter,
        write_note_fn=_notes,
        write_noise_fn=_noise,
        noise_coalescer=coalescer,
    )
    from alice_cozylobe.qwen_client import QwenClassification

    event = CozyHemEvent(
        kind="entity:update",
        entity_id="sensor.hue_kitchen_light_level",
        payload={"state": "100"},
        received_at=clock(),
    )
    classification = QwenClassification(
        urgency="LOW",
        intent="ambient_reading",
        summary="kitchen light_level rising",
        reasoning="circadian ramp",
        raw={},
    )
    # First event — buffered, no write yet.
    wake._backstop_note(event, classification)
    assert notes_writes == []
    assert noise_writes == []
    assert coalescer.pending_count("light_level") == 1
    # Buffered telemetry event was emitted.
    buffered = emitter.of_kind("cozylobe_noise_buffered")
    assert len(buffered) == 1


@pytest.mark.asyncio
async def test_wake_loop_backstop_flushes_light_level_at_threshold():
    """Three light_level backstop notes within the window → one
    coalesced noise write, no notes/ writes."""
    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    clock = _FakeClock()
    coalescer = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    emitter = CapturingEmitter()
    wake = WakeLoop(
        emitter=emitter,
        write_note_fn=_notes,
        write_noise_fn=_noise,
        noise_coalescer=coalescer,
    )
    from alice_cozylobe.qwen_client import QwenClassification

    classification = QwenClassification(
        urgency="LOW",
        intent="ambient_reading",
        summary="kitchen light_level rising",
        reasoning="circadian ramp",
        raw={},
    )
    for i in range(3):
        event = CozyHemEvent(
            kind="entity:update",
            entity_id="sensor.hue_kitchen_light_level",
            payload={"state": str(100 + i)},
            received_at=clock(),
        )
        clock.advance(5.0)
        wake._backstop_note(event, classification)

    # One coalesced noise write, no notes writes.
    assert len(noise_writes) == 1
    assert notes_writes == []
    # The body lists all three events.
    body = noise_writes[0]["body"]
    assert "3" in body  # count somewhere
    assert "burst-coalesce" in noise_writes[0]["tags"]
    # Coalesce-flushed telemetry was emitted.
    flushed = emitter.of_kind("cozylobe_noise_flushed")
    assert len(flushed) == 1
    assert flushed[0]["count"] == 3


@pytest.mark.asyncio
async def test_wake_loop_backstop_routes_signal_event_to_notes():
    """A backstop note for a light.* state change should hit the
    notes/ writer directly — high-signal, no coalescing."""
    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    wake = WakeLoop(
        emitter=CapturingEmitter(),
        write_note_fn=_notes,
        write_noise_fn=_noise,
    )
    from alice_cozylobe.qwen_client import QwenClassification

    event = CozyHemEvent(
        kind="entity:update",
        entity_id="light.living_room",
        payload={"state": "off"},
        received_at=1_000.0,
    )
    classification = QwenClassification(
        urgency="LOW",
        intent="user_action",
        summary="living room light turned off",
        reasoning="explicit toggle",
        raw={},
    )
    wake._backstop_note(event, classification)
    assert len(notes_writes) == 1
    assert noise_writes == []
    assert "cozylobe-backstop" in notes_writes[0]["tags"]


@pytest.mark.asyncio
async def test_wake_loop_flush_stale_noise_drains_buffer():
    """Periodic-tick analog: a single light_level event followed by
    70s silence then flush_stale_noise → one stale-flush noise write."""
    notes_writes: list[dict] = []
    noise_writes: list[dict] = []

    def _notes(body, *, slug, tags):
        notes_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/notes.md")

    def _noise(body, *, slug, tags):
        noise_writes.append({"slug": slug, "tags": tags})
        return Path("/tmp/noise.md")

    clock = _FakeClock()
    coalescer = BurstCoalescer(window_s=60.0, threshold=3, clock=clock)
    emitter = CapturingEmitter()
    wake = WakeLoop(
        emitter=emitter,
        write_note_fn=_notes,
        write_noise_fn=_noise,
        noise_coalescer=coalescer,
    )
    from alice_cozylobe.qwen_client import QwenClassification

    event = CozyHemEvent(
        kind="entity:update",
        entity_id="sensor.hue_office_humidity",
        payload={"state": "45.2"},
        received_at=clock(),
    )
    classification = QwenClassification(
        urgency="LOW",
        intent="ambient_reading",
        summary="humidity drifting up",
        reasoning="hvac cycle",
        raw={},
    )
    wake._backstop_note(event, classification)
    # Nothing written immediately.
    assert noise_writes == []
    # Window not elapsed yet — flush_stale is a no-op.
    wake.flush_stale_noise()
    assert noise_writes == []
    # Advance past the window and flush again.
    clock.advance(70.0)
    wake.flush_stale_noise()
    assert len(noise_writes) == 1
    assert "noise-stale-flush" in noise_writes[0]["tags"]
