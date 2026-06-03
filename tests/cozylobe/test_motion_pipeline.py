"""Tests for Phase 2 of the cozylobe motion-cortex pipeline (#379).

Covers the seven acceptance pieces from the prompt:

* INPUT_KINDS filter drops non-input events at the SSE level.
* Motion events bypass the throttle.
* Other INPUT_KINDS events pass through the throttle.
* MotionTrail rotates correctly at the configured window size.
* MotionQueue coalesces and flushes on window expiry.
* Security-class motion bypasses the queue.
* classify_motion_batch assembles the prompt with the correct sections.
* End-to-end: simulated motion event → trail update → batch → classify
  → note written.

The wake_loop and motion-pipeline pieces are exercised via the public
API; the lower-level building blocks (trail, queue, prompt builder) get
direct unit coverage so a regression in one layer doesn't have to be
debugged through the integration test.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pytest

from alice_cozylobe.cortex import load_vault
from alice_cozylobe.events import CozyHemEvent
from alice_cozylobe.motion import (
    MotionEvent,
    MotionInference,
    MotionPipeline,
    MotionQueue,
    MotionTrail,
    build_motion_prompt,
    classify_motion_batch,
    is_security_class,
)
from alice_cozylobe.throttle import Throttle
from alice_cozylobe.wake_loop import WakeLoop
from core.events import CapturingEmitter


# ---------------------------------------------------------------------------
# Helpers


class _FakeClock:
    """Deterministic monotonic clock so tests don't have to sleep."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StubQwen:
    """Stub LLMClient.complete that returns a canned positional inference."""

    def __init__(self, payload: Optional[dict] = None) -> None:
        self.payload = payload or {
            "current_room": "Kitchen",
            "confidence": 0.6,
            "person_hypothesis": "Jason",
            "person_confidence": 0.5,
            "next_room_hypothesis": "Office",
            "next_room_confidence": 0.4,
            "reasoning": "Bedroom motion at 10:10, kitchen at 10:15.",
        }
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> dict:
        self.calls.append(prompt)
        return self.payload


def _motion(
    entity_id: str = "hue_kitchen_motion",
    *,
    room: str = "Kitchen",
    timestamp: float = 1_000.0,
    state: str = "on",
) -> MotionEvent:
    return MotionEvent(
        timestamp=timestamp,
        entity_id=entity_id,
        state=state,
        room_id=room,
    )


def _cozyhem(
    kind: str = "motion_detected",
    entity_id: str = "hue_kitchen_motion",
    *,
    received_at: float = 1_000.0,
    payload: Optional[dict] = None,
) -> CozyHemEvent:
    return CozyHemEvent(
        kind=kind,
        entity_id=entity_id,
        payload=payload or {"state": "on"},
        received_at=received_at,
    )


def _make_throttle(tmp_path: Path, *, input_kinds: Optional[list[str]] = None) -> Throttle:
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    if input_kinds is not None:
        # Render the input_kinds list inline so the throttle's mtime
        # reload picks them up on first handle().
        lines = ["input_kinds:"]
        for k in input_kinds:
            lines.append(f"  - {k}")
        cfg_path.write_text("\n".join(lines) + "\n")
    return Throttle(config_path=cfg_path)


# ---------------------------------------------------------------------------
# MotionTrail


def test_trail_rotates_at_configured_size():
    """Append 30 events to a size-10 trail; the oldest 20 should be
    evicted and snapshot() returns only the last 10 in order."""
    trail = MotionTrail(max_size=10)
    for i in range(30):
        trail.append(_motion(entity_id=f"sensor_{i}", timestamp=float(i)))
    snap = trail.snapshot()
    assert len(snap) == 10
    assert [e.entity_id for e in snap] == [f"sensor_{i}" for i in range(20, 30)]


def test_trail_snapshot_is_immutable_tuple():
    trail = MotionTrail(max_size=4)
    trail.append(_motion())
    snap = trail.snapshot()
    assert isinstance(snap, tuple)
    # Mutating the trail does not retroactively change the snapshot.
    trail.append(_motion(entity_id="other"))
    assert len(snap) == 1


def test_trail_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        MotionTrail(max_size=0)
    with pytest.raises(ValueError):
        MotionTrail(max_size=-1)


# ---------------------------------------------------------------------------
# MotionQueue


def test_queue_does_not_flush_until_window_elapses():
    clock = _FakeClock()
    q = MotionQueue(batch_window_s=30.0, clock=clock)
    q.add(_motion(timestamp=clock()))
    assert not q.is_ready()
    clock.advance(15.0)
    q.add(_motion(timestamp=clock()))
    # Inside the 30s window, still not ready.
    assert not q.is_ready()


def test_queue_flushes_on_window_expiry():
    clock = _FakeClock()
    q = MotionQueue(batch_window_s=30.0, clock=clock)
    q.add(_motion(timestamp=clock()))
    clock.advance(31.0)
    assert q.is_ready()
    batch = q.take_batch()
    assert len(batch) == 1
    # Queue is empty after take_batch; next add restarts the window.
    assert not q.is_ready()


def test_queue_flushes_when_max_batch_size_reached():
    """Burst protection: even within the window, hitting max_batch_size
    flushes immediately. Prevents unbounded memory on a stuck sensor."""
    clock = _FakeClock()
    q = MotionQueue(batch_window_s=30.0, max_batch_size=3, clock=clock)
    q.add(_motion(timestamp=clock()))
    q.add(_motion(timestamp=clock()))
    assert not q.is_ready()
    q.add(_motion(timestamp=clock()))
    # Third event hits max_batch_size → ready even though window
    # hasn't elapsed.
    assert q.is_ready()
    batch = q.take_batch()
    assert len(batch) == 3


def test_queue_coalesces_burst_into_single_batch():
    """10 events in 5s should coalesce into one batch, flushed after
    the window elapses."""
    clock = _FakeClock()
    q = MotionQueue(batch_window_s=30.0, clock=clock)
    for i in range(10):
        clock.advance(0.5)
        q.add(_motion(entity_id=f"sensor_{i}", timestamp=clock()))
    assert len(q) == 10
    assert not q.is_ready()
    clock.advance(30.0)
    assert q.is_ready()
    batch = q.take_batch()
    assert len(batch) == 10


def test_queue_take_batch_when_empty_returns_empty_list():
    q = MotionQueue(clock=_FakeClock())
    assert q.take_batch() == []


def test_queue_rejects_nonpositive_params():
    with pytest.raises(ValueError):
        MotionQueue(batch_window_s=0)
    with pytest.raises(ValueError):
        MotionQueue(batch_window_s=-1)
    with pytest.raises(ValueError):
        MotionQueue(max_batch_size=0)


# ---------------------------------------------------------------------------
# Security-class predicate


def test_is_security_class_at_3am_is_true():
    """3am motion is security-class under the default night window."""
    # 2026-05-26 03:00:00 local time.
    ts = time.mktime((2026, 5, 26, 3, 0, 0, 0, 0, -1))
    assert is_security_class(_motion(timestamp=ts)) is True


def test_is_security_class_at_noon_is_false():
    """Noon motion is normal; the night window does not include it."""
    ts = time.mktime((2026, 5, 26, 12, 0, 0, 0, 0, -1))
    assert is_security_class(_motion(timestamp=ts)) is False


def test_is_security_class_at_23_30_is_true():
    """23:30 is past the night-start hour."""
    ts = time.mktime((2026, 5, 26, 23, 30, 0, 0, 0, -1))
    assert is_security_class(_motion(timestamp=ts)) is True


def test_is_security_class_with_non_midnight_window():
    """A window that doesn't cross midnight (08:00–10:00) excludes 11."""
    ts = time.mktime((2026, 5, 26, 11, 0, 0, 0, 0, -1))
    assert (
        is_security_class(
            _motion(timestamp=ts),
            night_start_hour=8,
            night_end_hour=10,
        )
        is False
    )
    ts = time.mktime((2026, 5, 26, 9, 0, 0, 0, 0, -1))
    assert (
        is_security_class(
            _motion(timestamp=ts),
            night_start_hour=8,
            night_end_hour=10,
        )
        is True
    )


# ---------------------------------------------------------------------------
# build_motion_prompt


def test_prompt_includes_three_sections():
    """The prompt must carry CURRENT_BATCH, MOTION_TRAIL, CORTEX_STATE
    so qwen has every input the design §3.1 calls for."""
    prompt = build_motion_prompt(
        batch=[_motion(entity_id="hue_kitchen_motion", room="Kitchen")],
        trail=[_motion(entity_id="hue_bedroom_motion", room="Bedroom")],
        vault=None,
    )
    assert "CURRENT_BATCH" in prompt
    assert "MOTION_TRAIL" in prompt
    assert "CORTEX_STATE" in prompt
    # And the expected fields in the output schema.
    assert "current_room" in prompt
    assert "next_room_hypothesis" in prompt
    assert "reasoning" in prompt


def test_prompt_serializes_batch_and_trail_as_json():
    batch_event = _motion(entity_id="hue_kitchen_motion", room="Kitchen", timestamp=42.0)
    trail_event = _motion(entity_id="hue_bedroom_motion", room="Bedroom", timestamp=12.0)
    prompt = build_motion_prompt(
        batch=[batch_event],
        trail=[trail_event],
        vault=None,
    )
    # Find each entity_id appears in the rendered JSON (compact format).
    assert "hue_kitchen_motion" in prompt
    assert "hue_bedroom_motion" in prompt
    # And the room titles are present so the classify call can ground them.
    assert "Kitchen" in prompt
    assert "Bedroom" in prompt


def test_prompt_uses_loaded_vault_for_cortex_snapshot(tmp_path: Path):
    """A populated vault should produce a non-empty rooms/sensors snapshot."""
    # Tiny vault: one room + one sensor pointing at it.
    rooms_dir = tmp_path / "rooms"
    sensors_dir = tmp_path / "sensors"
    rooms_dir.mkdir()
    sensors_dir.mkdir()
    # Wrap wikilinks in quotes so PyYAML doesn't parse [[..]] as a
    # nested list — matches the convention the onboarding CLI uses
    # (alice_cozylobe.cortex_cli.write_sensor_note / write_room_note).
    (rooms_dir / "Kitchen.md").write_text(
        "---\n"
        "title: Kitchen\n"
        "adjacent: \"[[rooms/Living Room]]\"\n"
        "---\n\n"
        "Kitchen note.\n"
    )
    (sensors_dir / "hue_kitchen_motion.md").write_text(
        "---\n"
        "title: hue_kitchen_motion\n"
        "room: \"[[rooms/Kitchen]]\"\n"
        "---\n\n"
        "Sensor note.\n"
    )
    vault = load_vault(tmp_path)
    prompt = build_motion_prompt(batch=[], trail=[], vault=vault)
    # Cortex section should be present and reference the room + sensor.
    assert "Kitchen" in prompt
    assert "hue_kitchen_motion" in prompt
    assert "Living Room" in prompt  # adjacency rendered


# ---------------------------------------------------------------------------
# classify_motion_batch


@pytest.mark.asyncio
async def test_classify_motion_batch_parses_inference():
    qwen = _StubQwen()
    result = await classify_motion_batch(
        batch=[_motion(entity_id="hue_kitchen_motion", room="Kitchen")],
        trail=[],
        llm_client=qwen,
        vault=None,
    )
    assert isinstance(result, MotionInference)
    assert result.current_room == "Kitchen"
    assert result.confidence == pytest.approx(0.6)
    assert result.person_hypothesis == "Jason"
    assert result.next_room_hypothesis == "Office"
    # The raw payload is preserved for downstream inspection.
    assert result.raw["reasoning"].startswith("Bedroom motion")
    # And the prompt qwen saw carried the three sections.
    assert len(qwen.calls) == 1
    assert "CURRENT_BATCH" in qwen.calls[0]


@pytest.mark.asyncio
async def test_classify_tolerates_missing_fields():
    """If qwen omits a field, classify_motion_batch returns None for it
    rather than crashing."""
    qwen = _StubQwen(payload={"reasoning": "sparse response"})
    result = await classify_motion_batch(
        batch=[_motion()],
        trail=[],
        llm_client=qwen,
        vault=None,
    )
    assert result.current_room is None
    assert result.confidence is None
    assert result.person_hypothesis is None
    assert result.reasoning == "sparse response"


# ---------------------------------------------------------------------------
# MotionPipeline.handle — security bypass + queue coalesce + note write


@pytest.mark.asyncio
async def test_security_class_bypasses_queue_and_writes_note():
    """Motion at 3am should classify immediately, NOT enqueue."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        write_note=_capture,
        # Force every event into security-class so the bypass fires.
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem(received_at=1_000.0))
    # Note was written, qwen was called, AND the queue is empty (bypass).
    assert len(notes) == 1
    assert len(qwen.calls) == 1
    assert len(pipeline.queue) == 0
    # The security tag is present so thinking can route it specially.
    assert "motion-security" in notes[0]["tags"]
    assert "motion-pipeline" in notes[0]["tags"]


@pytest.mark.asyncio
async def test_normal_motion_event_is_queued_not_classified():
    """A non-security motion event should land in the queue without
    triggering a classify call."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    clock = _FakeClock()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        queue=MotionQueue(batch_window_s=30.0, clock=clock),
        write_note=_capture,
        security_predicate=lambda _e: False,
    )
    await pipeline.handle(_cozyhem(received_at=clock()))
    # Queue has the event; nothing else fired.
    assert len(pipeline.queue) == 1
    assert qwen.calls == []
    assert notes == []


@pytest.mark.asyncio
async def test_queue_flushes_classifies_and_writes_note_after_window():
    """Add events, advance the clock past the window, add one more —
    the pipeline should flush, classify, and write one note for the
    full batch."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    clock = _FakeClock()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        queue=MotionQueue(batch_window_s=30.0, clock=clock),
        write_note=_capture,
        security_predicate=lambda _e: False,
        clock=clock,
    )
    # Two events inside the window.
    await pipeline.handle(_cozyhem(received_at=clock()))
    clock.advance(5.0)
    await pipeline.handle(_cozyhem(received_at=clock()))
    assert qwen.calls == []  # still inside window

    # Advance past the window and add one more event — flush fires.
    clock.advance(31.0)
    await pipeline.handle(_cozyhem(received_at=clock()))
    assert len(qwen.calls) == 1
    assert len(notes) == 1
    # Note carries the motion-pipeline tag.
    assert "motion-pipeline" in notes[0]["tags"]
    # Body mentions current room from the stubbed inference.
    assert "Kitchen" in notes[0]["body"]


@pytest.mark.asyncio
async def test_pipeline_writes_degraded_note_when_qwen_unreachable():
    """When LLMClient.complete raises LLMUnreachable, the pipeline
    should write a 'motion-degraded' note so the event still leaves a
    trail."""
    from core.llm_client import LLMUnreachable

    class _BrokenQwen:
        async def complete(self, prompt: str) -> dict:
            raise LLMUnreachable("desk unreachable")

    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    pipeline = MotionPipeline(
        llm_client=_BrokenQwen(),
        write_note=_capture,
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem(received_at=1_000.0))
    assert len(notes) == 1
    assert "motion-degraded" in notes[0]["tags"]
    assert "qwen_unreachable" in notes[0]["body"]


@pytest.mark.asyncio
async def test_pipeline_writes_degraded_note_when_no_qwen_wired():
    """If no qwen client is wired (link-loss equivalent), the pipeline
    still appends to the trail and writes a degraded note rather than
    crashing."""
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    pipeline = MotionPipeline(
        llm_client=None,
        write_note=_capture,
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem(received_at=1_000.0))
    assert len(notes) == 1
    assert "motion-degraded" in notes[0]["tags"]
    assert "qwen_disabled" in notes[0]["body"]


@pytest.mark.asyncio
async def test_pipeline_resolves_room_from_vault(tmp_path: Path):
    """The pipeline should populate MotionEvent.room_id from the
    cortex sensor → room lookup so the prompt carries the room
    name, not just the entity_id."""
    rooms_dir = tmp_path / "rooms"
    sensors_dir = tmp_path / "sensors"
    rooms_dir.mkdir()
    sensors_dir.mkdir()
    (rooms_dir / "Kitchen.md").write_text(
        "---\ntitle: Kitchen\n---\n\nKitchen.\n"
    )
    (sensors_dir / "hue_kitchen_motion.md").write_text(
        "---\n"
        "title: hue_kitchen_motion\n"
        "room: \"[[rooms/Kitchen]]\"\n"
        "---\n\n"
    )
    vault = load_vault(tmp_path)

    qwen = _StubQwen()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=vault,
        write_note=lambda *a, **kw: Path("/tmp/x.md"),
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(
        _cozyhem(entity_id="hue_kitchen_motion", received_at=1_000.0)
    )
    # The trail entry should carry the resolved room.
    snap = pipeline.trail.snapshot()
    assert snap[-1].room_id == "Kitchen"
    # And the prompt qwen saw mentions the room title.
    assert "Kitchen" in qwen.calls[0]


# ---------------------------------------------------------------------------
# WakeLoop — INPUT_KINDS filter + motion routing


@pytest.mark.asyncio
async def test_wake_loop_drops_non_input_kinds(tmp_path: Path):
    """entity:update (an OUTPUT kind) must be dropped before the
    throttle, before any classify, before any agent dispatch."""
    throttle = _make_throttle(tmp_path)  # default input_kinds
    emitter = CapturingEmitter()
    run_calls: list = []

    async def _stub_run_agent(*args, **kwargs):
        run_calls.append((args, kwargs))

    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        run_agent_fn=_stub_run_agent,
    )
    # entity:update — NOT on the INPUT_KINDS allowlist.
    await wake._handle_event(_cozyhem(kind="entity:update", payload={"brightness": 0.5}))
    # Filter event was emitted; nothing else fired.
    dropped = emitter.of_kind("cozylobe_event_dropped_non_input")
    assert len(dropped) == 1
    assert emitter.of_kind("cozylobe_event_received") == []
    assert run_calls == []


@pytest.mark.asyncio
async def test_wake_loop_passes_input_kind_through_throttle(tmp_path: Path):
    """A non-motion INPUT kind (button_pressed) should NOT be filtered
    out, should pass through the throttle (which fails-open on unknown
    kinds), and should reach the run_agent dispatch."""
    throttle = _make_throttle(tmp_path)  # default input_kinds
    emitter = CapturingEmitter()
    run_calls: list = []

    async def _stub_run_agent(*args, **kwargs):
        run_calls.append((args, kwargs))

    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        run_agent_fn=_stub_run_agent,
    )
    await wake._handle_event(_cozyhem(kind="button_pressed", entity_id="button.kitchen"))
    # No drop fired; the event made it past the filter.
    assert emitter.of_kind("cozylobe_event_dropped_non_input") == []
    assert len(emitter.of_kind("cozylobe_event_received")) == 1
    # And the agent dispatch ran.
    assert len(run_calls) == 1


@pytest.mark.asyncio
async def test_wake_loop_routes_motion_to_pipeline_bypassing_throttle(
    tmp_path: Path,
):
    """Motion events must NOT pass through the throttle at all — they
    route directly to the motion pipeline. We verify by checking the
    pipeline's queue receives the event AND the throttle is never
    consulted (no throttled telemetry, no run_agent dispatch)."""
    throttle = _make_throttle(tmp_path)
    emitter = CapturingEmitter()
    run_calls: list = []

    async def _stub_run_agent(*args, **kwargs):
        run_calls.append((args, kwargs))

    qwen = _StubQwen()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        write_note=lambda *a, **kw: Path("/tmp/x.md"),
        security_predicate=lambda _e: False,  # normal path → queue it
    )

    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        motion_pipeline=pipeline,
        run_agent_fn=_stub_run_agent,
    )
    await wake._handle_event(_cozyhem(kind="motion_detected"))

    # Motion got routed (telemetry event + pipeline state).
    assert len(emitter.of_kind("cozylobe_motion_routed")) == 1
    assert len(pipeline.queue) == 1
    # Throttle was not consulted (no throttled telemetry).
    assert emitter.of_kind("cozylobe_event_throttled") == []
    # Generic agent dispatch did NOT fire — motion follows its own path.
    assert run_calls == []


@pytest.mark.asyncio
async def test_wake_loop_motion_with_security_bypass_writes_note(
    tmp_path: Path,
):
    """End-to-end: motion event → trail update → security bypass →
    classify call → observation note written via the pipeline.

    Uses a forced-security predicate so the bypass fires deterministically
    without depending on wall-clock hour. The classify call uses the
    stub qwen.
    """
    throttle = _make_throttle(tmp_path)
    emitter = CapturingEmitter()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake.md")

    qwen = _StubQwen()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        write_note=_capture,
        security_predicate=lambda _e: True,
    )
    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        motion_pipeline=pipeline,
    )
    await wake._handle_event(_cozyhem(kind="motion_detected"))

    # The trail was updated.
    assert len(pipeline.trail) == 1
    # The classify call ran exactly once.
    assert len(qwen.calls) == 1
    # And a single observation note was written, tagged correctly.
    assert len(notes) == 1
    assert "motion-pipeline" in notes[0]["tags"]
    assert "motion-security" in notes[0]["tags"]


# ---------------------------------------------------------------------------
# Phase 1 dedup — research/2026-06-03-cozylobe-observation-redundancy.md


@pytest.mark.asyncio
async def test_dedup_skips_note_after_three_identical_state_hashes():
    """Same zone-state observation 4 times → first 3 notes written, the
    4th is suppressed (matches the last 3 hashes in the ring buffer).
    Non-security, non-actionable tier so the dedup gate engages."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"slug": slug, "tags": tags})
        return Path("/tmp/x.md")

    clock = _FakeClock()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        queue=MotionQueue(batch_window_s=30.0, clock=clock),
        write_note=_capture,
        security_predicate=lambda _e: False,
        clock=clock,
    )
    # Four identical flush cycles: enqueue one event, advance past the
    # window, enqueue a trailing event to force the flush. Each cycle
    # produces one identical batch (same entity_id + state) → same hash.
    for _ in range(4):
        await pipeline.handle(_cozyhem(received_at=clock()))
        clock.advance(31.0)
        await pipeline.handle(_cozyhem(received_at=clock()))
    # First 3 cycles wrote notes; the 4th was deduped.
    assert len(notes) == 3


@pytest.mark.asyncio
async def test_dedup_skips_when_inferred_state_repeats_with_varying_events():
    """The case PR #449 was supposed to handle but didn't (see
    research/2026-06-03-cozylobe-dedup-ineffective.md): four flush
    cycles where the underlying motion events DIFFER each cycle
    (different entity_ids and on/off states — simulating sensors
    cycling) but the inferred state is identical (stub qwen returns
    the same room / person / next_room every call). With the raw
    event set removed from the dedup hash, hashes 1-4 all match and
    the 4th note is suppressed."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"slug": slug, "tags": tags})
        return Path("/tmp/x.md")

    clock = _FakeClock()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        queue=MotionQueue(batch_window_s=30.0, clock=clock),
        write_note=_capture,
        security_predicate=lambda _e: False,
        clock=clock,
    )
    # Vary entity_id + state across cycles. Inferred state from
    # _StubQwen is fixed, so the 4 hashes collide → 4th note deduped.
    variants = [
        ("hue_kitchen_motion", "on"),
        ("hue_office_motion", "off"),
        ("hue_living_room_motion", "on"),
        ("hue_hallway_motion", "off"),
    ]
    for entity_id, state in variants:
        await pipeline.handle(
            _cozyhem(entity_id=entity_id, received_at=clock(), payload={"state": state})
        )
        clock.advance(31.0)
        await pipeline.handle(
            _cozyhem(entity_id=entity_id, received_at=clock(), payload={"state": state})
        )
    # First 3 cycles wrote notes; the 4th was deduped because the
    # inferred state hash matched the prior 3 even though the raw
    # event set differed every time.
    assert len(notes) == 3


# ---------------------------------------------------------------------------
# Burst test: 10 events in 5 seconds without dropping


@pytest.mark.asyncio
async def test_burst_ten_events_in_five_seconds_no_drops():
    """Issue #379 acceptance: queue must handle bursts (10 events in
    5s) without dropping. After the window expires, all 10 should land
    in a single classified batch."""
    qwen = _StubQwen()
    notes: list[dict] = []
    clock = _FakeClock()

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/x.md")

    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        queue=MotionQueue(batch_window_s=30.0, max_batch_size=100, clock=clock),
        write_note=_capture,
        security_predicate=lambda _e: False,
        clock=clock,
    )
    # 10 events in 5 seconds.
    for i in range(10):
        clock.advance(0.5)
        await pipeline.handle(_cozyhem(received_at=clock()))
    # Nothing flushed yet (within window).
    assert qwen.calls == []
    assert len(pipeline.queue) == 10
    # Advance past window and add one more — flush fires.
    clock.advance(30.0)
    await pipeline.handle(_cozyhem(received_at=clock()))
    assert len(qwen.calls) == 1
    assert len(notes) == 1
    # The trail saw all 11 events.
    assert len(pipeline.trail) == 11


# ---------------------------------------------------------------------------
# Throttle config — input_kinds parsing


def test_throttle_config_parses_input_kinds(tmp_path: Path):
    """A yaml with input_kinds: [...] should expose the list via
    throttle.config.input_kinds and Throttle.is_input_kind."""
    cfg = tmp_path / "cozylobe-throttle.yaml"
    cfg.write_text(
        "input_kinds:\n"
        "  - motion_detected\n"
        "  - button_pressed\n"
    )
    throttle = Throttle(config_path=cfg)
    # Force a reload pass.
    assert throttle.is_input_kind(_cozyhem(kind="motion_detected")) is True
    assert throttle.is_input_kind(_cozyhem(kind="button_pressed")) is True
    assert throttle.is_input_kind(_cozyhem(kind="entity:update")) is False


def test_throttle_default_input_kinds_includes_motion(tmp_path: Path):
    """Missing input_kinds in the yaml → default set applies."""
    throttle = Throttle(config_path=tmp_path / "missing.yaml")
    assert throttle.is_input_kind(_cozyhem(kind="motion_detected")) is True
    assert throttle.is_input_kind(_cozyhem(kind="doorbell_pressed")) is True
    # entity:update is NOT an input kind — it's the routine update bus.
    assert throttle.is_input_kind(_cozyhem(kind="entity:update")) is False


def test_throttle_empty_input_kinds_means_accept_all(tmp_path: Path):
    """A misconfigured empty list should fail open, not drop everything."""
    cfg = tmp_path / "cozylobe-throttle.yaml"
    cfg.write_text("input_kinds: []\n")
    throttle = Throttle(config_path=cfg)
    assert throttle.is_input_kind(_cozyhem(kind="entity:update")) is True
    assert throttle.is_input_kind(_cozyhem(kind="anything_at_all")) is True


# ---------------------------------------------------------------------------
# Issue #393 — entity:update routing to motion pipeline
#
# In production CozyHem emits motion events as kind=entity:update with
# a binary_sensor entity_id, NOT as a synthetic motion_detected kind.
# The pre-fix INPUT_KINDS filter silently dropped all of them. These
# tests pin both arms: (a) MotionPipeline.is_motion_event recognizes
# the entity_id shape, (b) the wake_loop's end-to-end flow routes the
# real-shape event to the motion pipeline (not the generic classify
# path).


def test_is_motion_event_recognizes_entity_update_motion_entity():
    """A kind=entity:update with a binary_sensor.*_motion entity_id is
    motion — even though the kind isn't motion_detected."""
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="binary_sensor.hue_hallway_1_motion",
        payload={"state": "on"},
        received_at=1_000.0,
    )
    assert MotionPipeline.is_motion_event(event) is True


def test_is_motion_event_recognizes_doubled_suffix_motion_entity():
    """Hue compound naming: binary_sensor.*_motion_sensor_motion is
    real and must match the *_motion_* pattern."""
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="binary_sensor.hue_master_closet_motion_sensor_motion",
        payload={"state": "on"},
        received_at=1_000.0,
    )
    assert MotionPipeline.is_motion_event(event) is True


def test_is_motion_event_rejects_entity_update_light():
    """entity:update on a light is not motion — the original classify
    path applies (or the throttle drops it as a micro-delta)."""
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="light.hue_kitchen_2.3",
        payload={"brightness": 0.4},
        received_at=1_000.0,
    )
    assert MotionPipeline.is_motion_event(event) is False


def test_is_motion_event_rejects_other_binary_sensor():
    """Not every binary_sensor entity_id is motion — e.g. a light-level
    sensor on the same Hue device is binary_sensor.*_light_level."""
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="binary_sensor.hue_pantry_light_level",
        payload={"state": "on"},
        received_at=1_000.0,
    )
    assert MotionPipeline.is_motion_event(event) is False


def test_is_motion_event_still_matches_kind_arm():
    """motion_detected (kept around for future producers) still
    matches via the kind arm, regardless of entity_id."""
    event = CozyHemEvent(
        kind="motion_detected",
        entity_id="anything",
        payload={},
        received_at=1_000.0,
    )
    assert MotionPipeline.is_motion_event(event) is True


@pytest.mark.asyncio
async def test_wake_loop_routes_entity_update_motion_event_to_pipeline(
    tmp_path: Path,
):
    """End-to-end #393 acceptance: a kind=entity:update event from a
    motion-sensor entity_id passes the INPUT filter AND routes to the
    motion pipeline (not the generic classify path)."""
    throttle = _make_throttle(tmp_path)  # default config — includes patterns
    emitter = CapturingEmitter()
    run_calls: list = []

    async def _stub_run_agent(*args, **kwargs):
        run_calls.append((args, kwargs))

    qwen = _StubQwen()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        write_note=lambda *a, **kw: Path("/tmp/x.md"),
        security_predicate=lambda _e: False,
    )
    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        motion_pipeline=pipeline,
        run_agent_fn=_stub_run_agent,
    )
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="binary_sensor.hue_hallway_1_motion",
        payload={"state": "on"},
        received_at=1_000.0,
    )
    await wake._handle_event(event)

    # The input filter did NOT drop it.
    assert emitter.of_kind("cozylobe_event_dropped_non_input") == []
    # And the motion pipeline took it (not the generic classify).
    assert len(emitter.of_kind("cozylobe_motion_routed")) == 1
    assert len(pipeline.queue) == 1
    assert run_calls == []


@pytest.mark.asyncio
async def test_wake_loop_drops_entity_update_for_light(tmp_path: Path):
    """The behavior the issue's prior code already had — entity:update
    on a light entity drops as an OUTPUT event. Pinning the no-regression
    boundary."""
    throttle = _make_throttle(tmp_path)
    emitter = CapturingEmitter()

    async def _stub_run_agent(*args, **kwargs):  # pragma: no cover
        raise AssertionError("agent dispatch must not fire")

    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        run_agent_fn=_stub_run_agent,
    )
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="light.hue_kitchen_2.3",
        payload={"brightness": 0.4},
        received_at=1_000.0,
    )
    await wake._handle_event(event)
    assert len(emitter.of_kind("cozylobe_event_dropped_non_input")) == 1
    assert emitter.of_kind("cozylobe_event_received") == []


@pytest.mark.asyncio
async def test_wake_loop_drops_entity_update_for_non_motion_binary_sensor(
    tmp_path: Path,
):
    """A binary_sensor that isn't on the pattern allowlist (e.g.
    light_level) must still drop. Verifies the allowlist is honored
    rather than waved through for any binary_sensor.* shape."""
    throttle = _make_throttle(tmp_path)
    emitter = CapturingEmitter()

    async def _stub_run_agent(*args, **kwargs):  # pragma: no cover
        raise AssertionError("agent dispatch must not fire")

    wake = WakeLoop(
        emitter=emitter,
        throttle=throttle,
        run_agent_fn=_stub_run_agent,
    )
    event = CozyHemEvent(
        kind="entity:update",
        entity_id="binary_sensor.hue_pantry_light_level",
        payload={"state": "on"},
        received_at=1_000.0,
    )
    await wake._handle_event(event)
    assert len(emitter.of_kind("cozylobe_event_dropped_non_input")) == 1
    assert emitter.of_kind("cozylobe_event_received") == []
