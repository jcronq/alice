"""Tests for the cozylobe trail-based breach classifier.

Replaces the legacy ``person=unknown AND security=true`` actionable
trigger with the four-case spec Jason laid out on Signal 2026-05-28
08:21-08:24 EDT. Coverage:

* Pure ``classify_trail`` function — each of the four cases (ingress
  trail, bedroom origin, isolated PIR, middle-of-house propagation).
* Top-level ``is_breach_event`` — alarm-state gating.
* ``AlarmStateCache`` — polling, fail-open on errors, cached value
  served between polls.
* Motion pipeline end-to-end — verifies the buggy "every nighttime
  event becomes actionable" path is dead AND the new path surfaces
  the right cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from alice_cozylobe.breach import (
    AlarmStateCache,
    classify_trail,
    is_breach_event,
)
from alice_cozylobe.motion import MotionEvent, MotionPipeline


# ---------------------------------------------------------------------------
# Helpers


# Toy home graph that mirrors the real adjacency in
# ~/alice-mind/cozylobe-cortex/rooms/ for the rooms the tests use.
HOME_ADJACENCY: dict[str, set[str]] = {
    "Playroom": {"Theater", "Gym", "Basement Bathroom"},
    "Kitchen Hallway": {"Kitchen", "Dining Room", "Kitchen Bathroom", "Laundry Room"},
    "Kitchen": {"Kitchen Hallway", "Kitchen Dining Area", "Pantry"},
    "Kitchen Dining Area": {"Kitchen", "Living Room"},
    "Living Room": {"Kitchen Dining Area", "Entranceway", "Hallway"},
    "Entranceway": {"Living Room", "Dining Room", "Library"},
    "Dining Room": {"Entranceway", "Kitchen Hallway"},
    "Hallway": {
        "Living Room",
        "Guest Bedroom",
        "Master Bedroom",
        "Nursery",
        "Kids Bathroom",
        "Office Hallway",
    },
    "Master Bedroom": {"Hallway", "Master Bathroom"},
    "Master Bathroom": {"Master Bedroom", "Master Closet"},
    "Nursery": {"Hallway", "Kids Bathroom"},
    "Office Hallway": {"Hallway", "Office Kitchen", "Office Bathroom", "Office"},
    "Office": {"Office Hallway"},
    "Laundry Room": {"Kitchen Hallway"},
    # Same-room transitions are handled by the chain walker itself,
    # not the adjacency map. So we don't need self-loops here.
}


def _ev(room: Optional[str], *, ts: float, entity: Optional[str] = None) -> MotionEvent:
    """Construct a :class:`MotionEvent`. Defaults ``entity_id`` to a
    sensor name derived from the room so each room in a multi-room
    trail produces a distinct ``entity_id`` (matters for the
    isolated-PIR test).
    """
    if entity is None:
        if room is None:
            entity = "binary_sensor.unknown_motion"
        else:
            slug = room.lower().replace(" ", "_")
            entity = f"binary_sensor.hue_{slug}_motion"
    return MotionEvent(
        timestamp=ts,
        entity_id=entity,
        state="on",
        room_id=room,
    )


# ---------------------------------------------------------------------------
# classify_trail — pure decision logic


def test_classify_trail_ingress_to_kitchen_is_actionable():
    """Front door → living room → kitchen within 5 minutes is the
    canonical breach signature. Ingress at Entranceway."""
    # ts in seconds, walking through the house.
    trail = [
        _ev("Entranceway", ts=100.0),
        _ev("Living Room", ts=130.0),
        _ev("Kitchen Dining Area", ts=160.0),
    ]
    current = _ev("Kitchen", ts=200.0)
    result = classify_trail(current, trail, HOME_ADJACENCY)
    assert result.actionable is True
    assert result.case == "ingress_trail"
    assert "Entranceway" in result.reason


def test_classify_trail_basement_walkout_is_actionable():
    """Playroom (basement walkout) → Kitchen via the stair landing in
    Kitchen Hallway is also an ingress trail."""
    trail = [
        _ev("Playroom", ts=0.0),
        _ev("Kitchen Hallway", ts=60.0),
    ]
    # Playroom isn't adjacent to Kitchen Hallway in the registry
    # (basement-to-main is a stair transition, not a structural edge),
    # so the chain walker would reject. The test ensures that even
    # without that intermediate adjacency, the simpler "ingress room
    # fired in the lookback" signal still trips. We give it a chain
    # via the kitchen-ecosystem rooms instead.
    current = _ev("Kitchen", ts=120.0)
    # Skip the chain-walk check by passing a None adjacency (degrades
    # to time-only chain) — verifies the case still fires when the
    # graph wouldn't let us connect the rooms structurally.
    result = classify_trail(current, trail, adjacency=None)
    assert result.actionable is True
    assert result.case == "ingress_trail"


def test_classify_trail_bedroom_origin_is_silent():
    """Jason gets up at 03:00, walks Master Bedroom → Hallway → Kitchen
    for water. Bedroom-originated interior movement is silent."""
    trail = [
        _ev("Master Bedroom", ts=0.0),
        _ev("Hallway", ts=30.0),
        _ev("Living Room", ts=60.0),
        _ev("Kitchen Dining Area", ts=90.0),
    ]
    current = _ev("Kitchen", ts=120.0)
    result = classify_trail(current, trail, HOME_ADJACENCY)
    assert result.actionable is False
    assert result.case == "interior_origin"
    assert "Master Bedroom" in result.reason


def test_classify_trail_hallway_origin_is_silent():
    """Trail that starts in a hallway is treated as interior movement
    even if no bedroom event is in the lookback window."""
    trail = [
        _ev("Hallway", ts=0.0),
        _ev("Living Room", ts=30.0),
    ]
    current = _ev("Kitchen Dining Area", ts=60.0)
    result = classify_trail(current, trail, HOME_ADJACENCY)
    assert result.actionable is False
    assert result.case == "interior_origin"


def test_classify_trail_isolated_pir_is_silent():
    """Single firing in middle-of-house with no other sensors in the
    ±2 minute window = PIR misfire."""
    # Empty trail. The classifier should still see a single sensor in
    # the isolated window and silence it.
    current = _ev("Kitchen", ts=1_000.0)
    result = classify_trail(current, [], HOME_ADJACENCY)
    assert result.actionable is False
    assert result.case == "isolated_pir"


def test_classify_trail_isolated_pir_with_self_in_window_is_silent():
    """Even if the trail contains older firings of the SAME sensor,
    the isolated check should still trip on distinct-sensor count."""
    trail = [
        # Same sensor, twice (e.g. retriggered after the lockout).
        _ev("Kitchen", ts=900.0),
    ]
    current = _ev("Kitchen", ts=1_000.0)
    result = classify_trail(current, trail, HOME_ADJACENCY)
    assert result.actionable is False
    assert result.case == "isolated_pir"


def test_classify_trail_middle_of_house_propagation_is_actionable():
    """Two distinct middle-of-house sensors in the ±2 min window AND
    no ingress in the lookback = someone is moving around and we
    don't know how they got in."""
    trail = [
        _ev("Kitchen", ts=900.0, entity="binary_sensor.hue_kitchen_motion"),
        _ev(
            "Living Room",
            ts=950.0,
            entity="binary_sensor.hue_living_room_motion",
        ),
    ]
    current = _ev(
        "Kitchen Dining Area",
        ts=1_000.0,
        entity="binary_sensor.hue_kitchen_dining_motion",
    )
    result = classify_trail(current, trail, HOME_ADJACENCY)
    assert result.actionable is True
    assert result.case == "middle_of_house_propagation"


def test_classify_trail_sustained_one_room_occupancy_is_silent():
    """Multiple firings of the SAME sensor — someone standing in the
    kitchen — should not propagate."""
    # Three distinct sensors might be needed for a propagation case,
    # but this test uses one sensor + one room → silent fallback.
    trail = [
        _ev("Kitchen", ts=900.0, entity="binary_sensor.hue_kitchen_motion"),
        _ev("Kitchen", ts=950.0, entity="binary_sensor.hue_kitchen_motion"),
    ]
    current = _ev("Kitchen", ts=1_000.0, entity="binary_sensor.hue_kitchen_motion")
    result = classify_trail(current, trail, HOME_ADJACENCY)
    # All same sensor → still isolated-PIR by sensor count.
    assert result.actionable is False
    assert result.case == "isolated_pir"


# ---------------------------------------------------------------------------
# is_breach_event — alarm-state gating


def test_is_breach_event_disarmed_is_always_silent():
    """Alarm disarmed → ingress trail still silent."""
    trail = [_ev("Entranceway", ts=100.0)]
    current = _ev("Kitchen", ts=200.0)
    result = is_breach_event(
        current, trail, alarm_state="disarmed", adjacency=HOME_ADJACENCY
    )
    assert result.actionable is False
    assert result.case == "alarm_not_armed"


def test_is_breach_event_armed_home_with_bedroom_origin_is_silent():
    """Armed but trail starts in Master Bedroom — interior wandering."""
    trail = [
        _ev("Master Bedroom", ts=0.0),
        _ev("Hallway", ts=60.0),
    ]
    current = _ev("Living Room", ts=120.0)
    result = is_breach_event(
        current, trail, alarm_state="armed_home", adjacency=HOME_ADJACENCY
    )
    assert result.actionable is False
    assert result.case == "interior_origin"


def test_is_breach_event_armed_home_with_ingress_trail_is_actionable():
    """Armed home, front-door breach in progress → actionable."""
    trail = [
        _ev("Entranceway", ts=0.0),
        _ev("Living Room", ts=30.0),
    ]
    current = _ev("Kitchen Dining Area", ts=60.0)
    result = is_breach_event(
        current, trail, alarm_state="armed_home", adjacency=HOME_ADJACENCY
    )
    assert result.actionable is True
    assert result.case == "ingress_trail"


def test_is_breach_event_armed_home_with_isolated_pir_is_silent():
    """The 3:41 EDT Kitchen event that triggered this whole rewrite."""
    current = _ev("Kitchen", ts=1_000.0)
    result = is_breach_event(
        current, [], alarm_state="armed_home", adjacency=HOME_ADJACENCY
    )
    assert result.actionable is False
    assert result.case == "isolated_pir"


def test_is_breach_event_armed_away_with_propagation_is_actionable():
    """Nobody is supposed to be home and the trail shows two distinct
    middle-of-house sensors moving."""
    trail = [
        _ev(
            "Kitchen",
            ts=900.0,
            entity="binary_sensor.hue_kitchen_motion",
        ),
        _ev(
            "Living Room",
            ts=950.0,
            entity="binary_sensor.hue_living_room_motion",
        ),
    ]
    current = _ev(
        "Kitchen Dining Area",
        ts=1_000.0,
        entity="binary_sensor.hue_kitchen_dining_motion",
    )
    result = is_breach_event(
        current, trail, alarm_state="armed_away", adjacency=HOME_ADJACENCY
    )
    assert result.actionable is True
    assert result.case == "middle_of_house_propagation"


# ---------------------------------------------------------------------------
# AlarmStateCache


class _ClockStub:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def test_alarm_state_cache_polls_and_caches(tmp_path: Path):
    token_file = tmp_path / "ha_token"
    token_file.write_text("test-token\n")
    calls: list[tuple[str, str]] = []

    def _fake_get(url: str, token: str) -> tuple[int, dict]:
        calls.append((url, token))
        return 200, {
            "entity_id": "alarm_control_panel.home_alarm",
            "state": "armed_home",
        }

    clock = _ClockStub(start=1000.0)
    cache = AlarmStateCache(
        ha_url="http://test-ha:8123",
        token_path=token_file,
        poll_interval_s=60.0,
        http_get_fn=_fake_get,
        clock=clock,
    )
    # First poll: state changes from "unknown" → "armed_home".
    assert cache.state == "unknown"
    state = cache.maybe_poll()
    assert state == "armed_home"
    assert cache.state == "armed_home"
    assert len(calls) == 1
    # Token + URL plumbing
    assert calls[0][0].endswith("/api/states/alarm_control_panel.home_alarm")
    assert calls[0][1] == "test-token"

    # Within the poll interval: no additional HTTP call.
    clock.advance(30.0)
    state = cache.maybe_poll()
    assert state == "armed_home"
    assert len(calls) == 1

    # After the interval: another poll fires.
    clock.advance(40.0)
    state = cache.maybe_poll()
    assert len(calls) == 2


def test_alarm_state_cache_fail_open_on_http_error(tmp_path: Path):
    token_file = tmp_path / "ha_token"
    token_file.write_text("test-token\n")

    def _flaky_get(url: str, token: str) -> tuple[int, dict]:
        raise RuntimeError("HA down")

    clock = _ClockStub(start=1000.0)
    cache = AlarmStateCache(
        ha_url="http://test-ha:8123",
        token_path=token_file,
        poll_interval_s=60.0,
        http_get_fn=_flaky_get,
        clock=clock,
    )
    state = cache.poll()
    # Cache stays at the conservative "unknown" default after an error.
    assert state == "unknown"
    assert cache.state == "unknown"


def test_alarm_state_cache_fail_open_on_missing_token(tmp_path: Path):
    """Missing token file → no poll attempt, last-known state retained."""
    token_file = tmp_path / "nope_does_not_exist"

    def _should_not_be_called(url: str, token: str) -> tuple[int, dict]:
        raise AssertionError("HTTP should not be called without a token")

    cache = AlarmStateCache(
        token_path=token_file,
        http_get_fn=_should_not_be_called,
    )
    assert cache.poll() == "unknown"


def test_alarm_state_cache_retains_previous_state_on_subsequent_failure(
    tmp_path: Path,
):
    """A successful poll followed by a failure should keep the
    successful state — fail-open on transient HA hiccups."""
    token_file = tmp_path / "ha_token"
    token_file.write_text("test-token\n")
    responses = [
        (200, {"state": "armed_away"}),
        Exception("HA blip"),
    ]

    def _stepped_get(url: str, token: str) -> tuple[int, dict]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    clock = _ClockStub(start=1000.0)
    cache = AlarmStateCache(
        token_path=token_file,
        poll_interval_s=60.0,
        http_get_fn=_stepped_get,
        clock=clock,
    )
    assert cache.poll() == "armed_away"
    clock.advance(120.0)
    # Second poll throws; cache retains armed_away.
    assert cache.poll() == "armed_away"


# ---------------------------------------------------------------------------
# Motion pipeline end-to-end


class _CannedQwen:
    """Stand-in for QwenClient.complete that returns a fixed
    positional inference. Confidence is high enough (0.85) to clear
    the surface threshold's silent floor."""

    def __init__(self, *, room: str = "Kitchen") -> None:
        self.room = room
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> dict:
        self.calls.append(prompt)
        return {
            "current_room": self.room,
            "confidence": 0.85,
            "person_hypothesis": None,  # unknown person — the buggy default
            "person_confidence": 0.0,
            "next_room_hypothesis": None,
            "next_room_confidence": None,
            "reasoning": f"motion detected in {self.room}",
        }


def _make_pipeline(
    tmp_path: Path,
    *,
    alarm_state: str = "armed_home",
    qwen_room: str = "Kitchen",
):
    """Spin up a MotionPipeline wired with the breach classifier, a
    canned alarm cache, and capturing writers."""
    captured: dict[str, list] = {"notes": [], "surfaces": []}

    def _capture_note(body, *, slug, tags):
        captured["notes"].append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    def _capture_surface(body, *, slug, surface_type, extra_frontmatter=None):
        captured["surfaces"].append(
            {
                "body": body,
                "slug": slug,
                "surface_type": surface_type,
                "extra_frontmatter": extra_frontmatter or {},
            }
        )
        return Path("/tmp/fake-surface.md")

    # Stub alarm cache — bypass HTTP entirely.
    class _StubAlarmCache:
        def maybe_poll(self):
            return alarm_state

        @property
        def state(self):
            return alarm_state

    qwen = _CannedQwen(room=qwen_room)
    pipeline = MotionPipeline(
        qwen_client=qwen,
        vault=None,  # adjacency comes from the classify_fn override
        vault_root=tmp_path,
        write_note=_capture_note,
        write_surface=_capture_surface,
        breach_cache=_StubAlarmCache(),
        # Wire the classifier with the test home-graph by wrapping
        # is_breach_event in a closure that injects the adjacency.
        breach_classify_fn=lambda current, trail, *, alarm_state, adjacency=None: (
            is_breach_event(
                current,
                trail,
                alarm_state=alarm_state,
                adjacency=HOME_ADJACENCY,
            )
        ),
        # Use ``security_predicate`` to keep the legacy night-hours
        # path off — the new path is the only thing under test.
        security_predicate=lambda _e: False,
    )
    return pipeline, captured


@pytest.mark.asyncio
async def test_pipeline_disarmed_kitchen_event_no_actionable_surface(tmp_path: Path):
    """Disarmed alarm + middle-of-house Kitchen event = no surface,
    regardless of unknown-person guess."""
    from alice_cozylobe.events import CozyHemEvent

    pipeline, captured = _make_pipeline(tmp_path, alarm_state="disarmed")
    event = CozyHemEvent(
        kind="motion_detected",
        entity_id="binary_sensor.hue_kitchen_motion",
        payload={"state": "on"},
        received_at=1_000.0,
    )
    await pipeline.handle(event)
    # Note still goes out (logging), but NO actionable surface.
    assert captured["surfaces"] == []


@pytest.mark.asyncio
async def test_pipeline_armed_home_bedroom_origin_no_actionable_surface(
    tmp_path: Path,
):
    """Armed home + bedroom-origin trail = no actionable surface."""
    pipeline, captured = _make_pipeline(tmp_path, alarm_state="armed_home")
    # Manually inject into the trail since the MotionEvent.from_cozyhem
    # path can't resolve room without a vault.
    pipeline.trail.append(
        MotionEvent(
            timestamp=900.0,
            entity_id="binary_sensor.hue_master_bedroom_motion",
            state="on",
            room_id="Master Bedroom",
        )
    )
    pipeline.trail.append(
        MotionEvent(
            timestamp=930.0,
            entity_id="binary_sensor.hue_hallway_1_motion",
            state="on",
            room_id="Hallway",
        )
    )

    async def handle_with_room():
        # Manually invoke the same flow but inject the current event's
        # room. The simplest path is to call the lower-level method.
        motion = MotionEvent(
            timestamp=1_000.0,
            entity_id="binary_sensor.hue_kitchen_motion",
            state="on",
            room_id="Kitchen",
        )
        pipeline.trail.append(motion)
        pipeline._latest_breach = pipeline._classify_breach(motion)
        # Force the classify path (would normally hit the queue first)
        await pipeline._classify_and_write([motion], security=False)

    await handle_with_room()
    assert captured["surfaces"] == []
    # The latest breach result should be the bedroom-origin case.
    assert pipeline._latest_breach is not None
    assert pipeline._latest_breach.case == "interior_origin"


@pytest.mark.asyncio
async def test_pipeline_armed_home_ingress_trail_emits_actionable(tmp_path: Path):
    """Armed home + Entranceway → Living Room → Kitchen = actionable."""
    pipeline, captured = _make_pipeline(tmp_path, alarm_state="armed_home")
    pipeline.trail.append(
        MotionEvent(
            timestamp=900.0,
            entity_id="binary_sensor.hue_entranceway_motion",
            state="on",
            room_id="Entranceway",
        )
    )
    pipeline.trail.append(
        MotionEvent(
            timestamp=940.0,
            entity_id="binary_sensor.hue_living_room_motion",
            state="on",
            room_id="Living Room",
        )
    )
    motion = MotionEvent(
        timestamp=1_000.0,
        entity_id="binary_sensor.hue_kitchen_motion",
        state="on",
        room_id="Kitchen Dining Area",
    )
    pipeline.trail.append(motion)
    pipeline._latest_breach = pipeline._classify_breach(motion)
    await pipeline._classify_and_write([motion], security=False)

    assert len(captured["surfaces"]) == 1
    surface = captured["surfaces"][0]
    assert surface["surface_type"] == "cozylobe-actionable"
    assert surface["extra_frontmatter"].get("breach_case") == "ingress_trail"


@pytest.mark.asyncio
async def test_pipeline_armed_home_isolated_pir_no_actionable_surface(
    tmp_path: Path,
):
    """The exact case that fired five false positives overnight
    2026-05-27→28: Kitchen motion at 3:41 EDT with no other sensors,
    alarm armed, person=unknown."""
    pipeline, captured = _make_pipeline(tmp_path, alarm_state="armed_home")
    motion = MotionEvent(
        timestamp=1_000.0,
        entity_id="binary_sensor.hue_kitchen_motion",
        state="on",
        room_id="Kitchen",
    )
    pipeline.trail.append(motion)
    pipeline._latest_breach = pipeline._classify_breach(motion)
    await pipeline._classify_and_write([motion], security=False)

    assert captured["surfaces"] == []
    assert pipeline._latest_breach is not None
    assert pipeline._latest_breach.case == "isolated_pir"


@pytest.mark.asyncio
async def test_pipeline_armed_home_middle_of_house_propagation_is_actionable(
    tmp_path: Path,
):
    """Two distinct middle-of-house sensors fire within 2 min and no
    ingress in lookback → actionable."""
    pipeline, captured = _make_pipeline(tmp_path, alarm_state="armed_home")
    pipeline.trail.append(
        MotionEvent(
            timestamp=900.0,
            entity_id="binary_sensor.hue_kitchen_motion",
            state="on",
            room_id="Kitchen",
        )
    )
    pipeline.trail.append(
        MotionEvent(
            timestamp=950.0,
            entity_id="binary_sensor.hue_living_room_motion",
            state="on",
            room_id="Living Room",
        )
    )
    motion = MotionEvent(
        timestamp=1_000.0,
        entity_id="binary_sensor.hue_kitchen_dining_motion",
        state="on",
        room_id="Kitchen Dining Area",
    )
    pipeline.trail.append(motion)
    pipeline._latest_breach = pipeline._classify_breach(motion)
    await pipeline._classify_and_write([motion], security=False)

    assert len(captured["surfaces"]) == 1
    surface = captured["surfaces"][0]
    assert surface["surface_type"] == "cozylobe-actionable"
    assert (
        surface["extra_frontmatter"].get("breach_case")
        == "middle_of_house_propagation"
    )
