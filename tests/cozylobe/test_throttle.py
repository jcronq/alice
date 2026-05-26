"""Tests for the diff-aware throttle (issue #371).

Covers the five acceptance pieces from the task spec:

* micro-delta suppression on tracked numeric fields
* on/off (and arbitrary string) state transitions always pass
* coalesce window emits a summary after the configured interval
* config reload mid-stream picks up new thresholds (mtime check)
* unknown event kinds pass through (fail-open)

Plus a handful of sanity checks: override semantics, missing config
file, malformed yaml, the bootstrap copy helper.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from alice_cozylobe.events import CozyHemEvent
from alice_cozylobe.throttle import (
    DEFAULT_TRACKED_INPUT_ENTITY_PATTERNS,
    SUMMARY_EVENT_KIND,
    Throttle,
    ThrottleConfig,
    ThrottleDecision,
    ensure_user_config,
)


# ---------------------------------------------------------------------------
# small helpers


def _make_event(
    entity_id: str = "light.office",
    kind: str = "entity:update",
    *,
    payload: dict | None = None,
    received_at: float = 0.0,
) -> CozyHemEvent:
    return CozyHemEvent(
        kind=kind,
        entity_id=entity_id,
        payload=payload or {},
        received_at=received_at,
    )


class _FakeClock:
    """Deterministic monotonic clock so tests don't have to sleep."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _new_throttle(tmp_path: pathlib.Path, clock: _FakeClock) -> Throttle:
    """Create a Throttle pointed at a non-existent path so defaults
    apply, and inject the fake clock."""
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    return Throttle(config_path=cfg_path, clock=clock)


# ---------------------------------------------------------------------------
# 1. micro-delta suppression


def test_micro_delta_after_first_seen_is_dropped(tmp_path):
    """First event establishes baseline; second event within ±0.05
    on brightness should be dropped (inside coalesce window)."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)

    first = _make_event(payload={"brightness": 0.14})
    second = _make_event(payload={"brightness": 0.12})

    assert throttle.handle(first).action == "pass"
    # Within the 5-min default window, second event drops.
    clock.advance(10.0)
    second_decision = throttle.handle(second)
    assert second_decision.action == "drop"
    assert second_decision.reason == "micro_delta"


def test_large_brightness_delta_passes(tmp_path):
    """0.14 → 0.50 is well above the 0.05 threshold; must pass."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"brightness": 0.14}))

    clock.advance(5.0)
    decision = throttle.handle(_make_event(payload={"brightness": 0.50}))
    assert decision.action == "pass"
    assert decision.reason == "non_micro_change"


def test_color_temp_micro_delta_also_suppressed(tmp_path):
    """color_temp is the second tracked field; same rule applies."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"color_temp": 0.40}))

    clock.advance(5.0)
    decision = throttle.handle(_make_event(payload={"color_temp": 0.42}))
    assert decision.action == "drop"


def test_diff_measured_against_last_emitted_not_last_raw(tmp_path):
    """Slow ramp: many small drops shouldn't drift unseen.

    If we measured against last RAW value, 1% per tick would never
    exceed the threshold. By measuring against last EMITTED, after
    enough accumulated drops the threshold is crossed and the event
    passes — restoring visibility for a gradual ramp.
    """
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"brightness": 1.0}))

    # Four ticks of -0.01: cumulative drop is 0.04, all individually
    # AND cumulatively below the 0.05 threshold → all drop.
    for i, val in enumerate([0.99, 0.98, 0.97, 0.96]):
        clock.advance(5.0)
        assert throttle.handle(_make_event(payload={"brightness": val})).action == "drop", (
            f"tick {i} val={val} should drop"
        )
    # 1.0 → 0.94 cumulative diff is 0.06 (comfortably above 0.05
    # without bumping into float precision) → must pass.
    clock.advance(5.0)
    assert throttle.handle(_make_event(payload={"brightness": 0.94})).action == "pass"


# ---------------------------------------------------------------------------
# 2. state transitions always pass


def test_state_transition_off_to_on_passes(tmp_path):
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"state": "off", "brightness": 0.0}))

    clock.advance(2.0)
    decision = throttle.handle(
        _make_event(payload={"state": "on", "brightness": 0.0})
    )
    assert decision.action == "pass"
    assert decision.reason == "state_transition"


def test_state_transition_passes_even_when_brightness_within_threshold(tmp_path):
    """An on→off with no brightness change must still pass (state
    transitions win over the micro-delta check)."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"state": "on", "brightness": 0.30}))

    clock.advance(1.0)
    decision = throttle.handle(
        _make_event(payload={"state": "off", "brightness": 0.30})
    )
    assert decision.action == "pass"
    assert decision.reason == "state_transition"


# ---------------------------------------------------------------------------
# 3. coalesce window emits a summary


def test_coalesce_window_emits_summary_event(tmp_path):
    """After 5 minutes of micro-deltas on one entity, the next
    micro-delta should emit a summary event carrying the suppressed
    count."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"brightness": 0.14}))

    # Three suppressed micro-deltas inside the window.
    for val in [0.13, 0.12, 0.11]:
        clock.advance(30.0)
        assert throttle.handle(_make_event(payload={"brightness": val})).action == "drop"

    # Cross the 5-min coalesce window from the last passthrough.
    clock.advance(305.0)
    decision = throttle.handle(_make_event(payload={"brightness": 0.10}))
    assert decision.action == "summary"
    assert decision.reason == "summary_window"
    assert decision.event.kind == SUMMARY_EVENT_KIND
    assert decision.event.payload["_suppressed_count"] == 4
    assert decision.event.payload["_summary"] is True
    assert decision.event.payload["_origin_kind"] == "entity:update"


def test_summary_resets_pending_count(tmp_path):
    """After a summary is emitted, the next micro-delta should drop
    (pending count starts from 1 again, window restarts)."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"brightness": 0.14}))

    clock.advance(10.0)
    throttle.handle(_make_event(payload={"brightness": 0.13}))
    clock.advance(305.0)
    summary = throttle.handle(_make_event(payload={"brightness": 0.12}))
    assert summary.action == "summary"

    clock.advance(10.0)
    next_event = throttle.handle(_make_event(payload={"brightness": 0.11}))
    assert next_event.action == "drop"


# ---------------------------------------------------------------------------
# 4. config reload mid-stream


def test_config_reload_on_mtime_change(tmp_path):
    """Write a yaml with a wide threshold, exercise it, then rewrite
    with a tighter threshold and confirm the new value applies on the
    next event."""
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    cfg_path.write_text(
        "tracked_numeric_fields:\n"
        "  brightness: 0.50\n"
        "coalesce_window_s: 300.0\n"
    )
    clock = _FakeClock()
    throttle = Throttle(config_path=cfg_path, clock=clock)

    # 0.14 → 0.40 (delta 0.26) is BELOW the wide 0.50 threshold → drop.
    throttle.handle(_make_event(payload={"brightness": 0.14}))
    clock.advance(1.0)
    assert (
        throttle.handle(_make_event(payload={"brightness": 0.40})).action
        == "drop"
    )

    # Rewrite the yaml with a tighter threshold. Bump the file mtime
    # so the throttle picks up the change on the next handle().
    cfg_path.write_text(
        "tracked_numeric_fields:\n"
        "  brightness: 0.05\n"
        "coalesce_window_s: 300.0\n"
    )
    # Force a different mtime — file systems with second-resolution
    # timestamps will otherwise see the two writes as identical.
    new_mtime = time.time() + 10
    import os

    os.utime(cfg_path, (new_mtime, new_mtime))

    # Establish a fresh baseline. The previous "drop" did not update
    # last_emitted, so last_emitted for this entity is still 0.14.
    # 0.14 → 0.30 (delta 0.16) is ABOVE the new 0.05 threshold → pass.
    clock.advance(5.0)
    decision = throttle.handle(_make_event(payload={"brightness": 0.30}))
    assert decision.action == "pass"
    # And the new threshold is reflected in the live config.
    assert throttle.config.tracked_numeric_fields["brightness"] == pytest.approx(0.05)


def test_reload_handles_malformed_yaml_gracefully(tmp_path, caplog):
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    cfg_path.write_text("tracked_numeric_fields: brightness: 0.05\n")  # broken
    clock = _FakeClock()
    throttle = Throttle(config_path=cfg_path, clock=clock)

    # Defaults stay in place; no crash.
    assert throttle.config.tracked_numeric_fields["brightness"] == pytest.approx(0.05)
    assert throttle.config.coalesce_window_s == pytest.approx(300.0)


def test_reload_handles_missing_file(tmp_path):
    """Missing yaml is the expected pre-bootstrap state; throttle
    falls back to code defaults and keeps running."""
    cfg_path = tmp_path / "does-not-exist.yaml"
    clock = _FakeClock()
    throttle = Throttle(config_path=cfg_path, clock=clock)

    decision = throttle.handle(_make_event(payload={"brightness": 0.14}))
    assert decision.action == "pass"


# ---------------------------------------------------------------------------
# 5. unknown event kinds pass through


def test_motion_detected_passes(tmp_path):
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    decision = throttle.handle(
        _make_event(kind="motion_detected", entity_id="motion.foyer")
    )
    assert decision.action == "pass"


def test_doorbell_passes(tmp_path):
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    decision = throttle.handle(
        _make_event(kind="doorbell_pressed", entity_id="doorbell.front")
    )
    assert decision.action == "pass"


def test_novel_unknown_kind_passes(tmp_path):
    """Fail-open: a producer that grows a new kind must not be
    silently dropped."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    decision = throttle.handle(
        _make_event(kind="some_brand_new_kind", entity_id="x.y")
    )
    assert decision.action == "pass"


def test_untracked_field_change_passes(tmp_path):
    """If an entity:update changes a field the throttle doesn't track
    (e.g. 'mode'), we should fail open and pass it through."""
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    throttle.handle(_make_event(payload={"brightness": 0.14, "mode": "relax"}))
    clock.advance(1.0)
    decision = throttle.handle(
        _make_event(payload={"brightness": 0.13, "mode": "focus"})
    )
    assert decision.action == "pass"
    assert decision.reason == "non_micro_change"


def test_first_event_for_entity_always_passes(tmp_path):
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    decision = throttle.handle(_make_event(payload={"brightness": 0.99}))
    assert decision.action == "pass"
    assert decision.reason == "first_seen"


# ---------------------------------------------------------------------------
# 6. overrides


def test_entity_override_disables_suppression(tmp_path):
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    cfg_path.write_text(
        "tracked_numeric_fields:\n"
        "  brightness: 0.05\n"
        "always_pass_entities:\n"
        "  - light.office\n"
    )
    clock = _FakeClock()
    throttle = Throttle(config_path=cfg_path, clock=clock)
    throttle.handle(_make_event(payload={"brightness": 0.14}))

    clock.advance(2.0)
    decision = throttle.handle(_make_event(payload={"brightness": 0.13}))
    assert decision.action == "pass"
    assert decision.reason == "override"


def test_no_entity_id_passes(tmp_path):
    clock = _FakeClock()
    throttle = _new_throttle(tmp_path, clock)
    decision = throttle.handle(_make_event(entity_id="", payload={"brightness": 0.5}))
    assert decision.action == "pass"
    assert decision.reason == "no_entity_id"


# ---------------------------------------------------------------------------
# 7. bootstrap helper


def test_ensure_user_config_copies_when_missing(tmp_path):
    shipped = tmp_path / "shipped.yaml"
    shipped.write_text("coalesce_window_s: 42.0\n")
    user = tmp_path / "subdir" / "user.yaml"

    copied = ensure_user_config(user_path=user, shipped_path=shipped)
    assert copied is True
    assert user.read_text() == "coalesce_window_s: 42.0\n"


def test_ensure_user_config_idempotent(tmp_path):
    shipped = tmp_path / "shipped.yaml"
    shipped.write_text("coalesce_window_s: 42.0\n")
    user = tmp_path / "user.yaml"
    user.write_text("coalesce_window_s: 99.0\n")  # user-edited

    copied = ensure_user_config(user_path=user, shipped_path=shipped)
    assert copied is False
    # Did not clobber the user's edits.
    assert user.read_text() == "coalesce_window_s: 99.0\n"


def test_ensure_user_config_handles_missing_shipped(tmp_path):
    """Missing shipped default should warn but not raise — the
    throttle still falls back to code defaults."""
    copied = ensure_user_config(
        user_path=tmp_path / "user.yaml",
        shipped_path=tmp_path / "does-not-exist.yaml",
    )
    assert copied is False


# ---------------------------------------------------------------------------
# 8. config parsing edge cases


def test_throttle_config_from_mapping_ignores_unknown_keys():
    """Forward compatibility: an agent might add a hint we don't yet
    read. Must not crash."""
    cfg = ThrottleConfig.from_mapping(
        {
            "tracked_numeric_fields": {"brightness": 0.10},
            "coalesce_window_s": 60.0,
            "future_field_we_dont_understand": ["a", "b"],
        }
    )
    assert cfg.tracked_numeric_fields == {"brightness": 0.10}
    assert cfg.coalesce_window_s == pytest.approx(60.0)


def test_throttle_config_from_mapping_rejects_non_mapping_root():
    cfg = ThrottleConfig.from_mapping(["not", "a", "dict"])
    # Falls back to defaults.
    assert cfg.tracked_numeric_fields == ThrottleConfig.default().tracked_numeric_fields


def test_throttle_decision_is_immutable():
    """ThrottleDecision is frozen so downstream consumers can rely on
    it as a stable record. Attempt to mutate raises."""
    import dataclasses

    d = ThrottleDecision("pass", _make_event(), reason="first_seen")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.action = "drop"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 9. Issue #393 — tracked_input_entity_patterns
#
# CozyHem emits motion-sensor events as kind=entity:update with the
# sensor's entity_id, not as a synthetic motion_detected kind. The
# kind-only INPUT_KINDS filter shipped in PR #385 dropped all of them
# (cozylobe.log evidence: 427 entity:update / 0 motion_detected). The
# fix adds an entity_id-pattern arm to is_input_kind so those events
# survive the filter.


def test_is_input_kind_passes_entity_update_for_motion_sensor(tmp_path):
    """entity:update on a binary_sensor.*_motion entity_id should pass
    the input filter via the pattern allowlist."""
    throttle = _new_throttle(tmp_path, _FakeClock())
    event = _make_event(
        kind="entity:update",
        entity_id="binary_sensor.hue_hallway_1_motion",
        payload={"state": "on"},
    )
    assert throttle.is_input_kind(event) is True


def test_is_input_kind_passes_doubled_suffix_motion_sensor(tmp_path):
    """Hue compound naming: binary_sensor.hue_master_closet_motion_sensor_motion
    must match the binary_sensor.*_motion_* pattern."""
    throttle = _new_throttle(tmp_path, _FakeClock())
    event = _make_event(
        kind="entity:update",
        entity_id="binary_sensor.hue_master_closet_motion_sensor_motion",
        payload={"state": "on"},
    )
    assert throttle.is_input_kind(event) is True


def test_is_input_kind_drops_entity_update_for_light(tmp_path):
    """entity:update on a light entity must still drop — no regression
    on the kind-only behavior for non-sensor entity_ids."""
    throttle = _new_throttle(tmp_path, _FakeClock())
    event = _make_event(
        kind="entity:update",
        entity_id="light.hue_kitchen_2.3",
        payload={"brightness": 0.4},
    )
    assert throttle.is_input_kind(event) is False


def test_is_input_kind_drops_non_matching_binary_sensor(tmp_path):
    """Not every binary_sensor entity_id qualifies — e.g. a light-level
    sensor isn't in the allowlist."""
    throttle = _new_throttle(tmp_path, _FakeClock())
    event = _make_event(
        kind="entity:update",
        entity_id="binary_sensor.hue_pantry_light_level",
        payload={"state": "on"},
    )
    assert throttle.is_input_kind(event) is False


def test_is_input_kind_with_empty_patterns_drops_entity_update(tmp_path):
    """Empty tracked_input_entity_patterns → no entity:update events
    qualify via the pattern arm. The kind-only #379 behavior is the
    regression-free fallback."""
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    cfg_path.write_text(
        "input_kinds:\n"
        "  - motion_detected\n"
        "tracked_input_entity_patterns: []\n"
    )
    throttle = Throttle(config_path=cfg_path, clock=_FakeClock())
    event = _make_event(
        kind="entity:update",
        entity_id="binary_sensor.hue_hallway_1_motion",
    )
    assert throttle.is_input_kind(event) is False


def test_is_input_kind_pattern_hot_reload(tmp_path):
    """Editing the yaml mid-stream picks up new entity patterns on the
    next event via the mtime check."""
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    # Start with NO entity patterns — entity:update drops.
    cfg_path.write_text(
        "input_kinds:\n"
        "  - motion_detected\n"
        "tracked_input_entity_patterns: []\n"
    )
    throttle = Throttle(config_path=cfg_path, clock=_FakeClock())
    event = _make_event(
        kind="entity:update",
        entity_id="binary_sensor.hue_hallway_1_motion",
    )
    assert throttle.is_input_kind(event) is False

    # Rewrite with the motion pattern — entity:update for a motion
    # sensor must now pass on the very next call.
    cfg_path.write_text(
        "input_kinds:\n"
        "  - motion_detected\n"
        "tracked_input_entity_patterns:\n"
        '  - "binary_sensor.*_motion"\n'
    )
    new_mtime = time.time() + 10
    import os

    os.utime(cfg_path, (new_mtime, new_mtime))

    assert throttle.is_input_kind(event) is True


def test_throttle_config_default_patterns_cover_all_classes():
    """Sanity: the shipped default covers motion / door / window /
    smoke / glass-break / water-leak / lock — the classes the issue
    lists. We don't enumerate exact patterns (those evolve), just
    verify representative entity_ids match."""
    import fnmatch as fn

    patterns = DEFAULT_TRACKED_INPUT_ENTITY_PATTERNS
    samples = [
        "binary_sensor.hue_hallway_1_motion",
        "binary_sensor.hue_master_closet_motion_sensor_motion",
        "binary_sensor.front_doorbell",
        "binary_sensor.kitchen_door",
        "binary_sensor.bedroom_window",
        "binary_sensor.kitchen_smoke",
        "binary_sensor.living_room_glass_break",
        "binary_sensor.basement_water_leak",
        "binary_sensor.front_lock",
    ]
    for entity_id in samples:
        assert any(
            fn.fnmatchcase(entity_id, p) for p in patterns
        ), f"no default pattern matched {entity_id}"


def test_throttle_config_parses_tracked_input_entity_patterns(tmp_path):
    """yaml → ThrottleConfig.tracked_input_entity_patterns round-trips."""
    cfg_path = tmp_path / "cozylobe-throttle.yaml"
    cfg_path.write_text(
        "tracked_input_entity_patterns:\n"
        '  - "binary_sensor.custom_*"\n'
        '  - "binary_sensor.another"\n'
    )
    cfg = ThrottleConfig.load(cfg_path)
    assert cfg.tracked_input_entity_patterns == frozenset(
        {"binary_sensor.custom_*", "binary_sensor.another"}
    )


def test_is_input_kind_kind_arm_still_works_with_patterns(tmp_path):
    """An event whose kind is on the input_kinds list still passes
    regardless of entity_id (the original #379 arm)."""
    throttle = _new_throttle(tmp_path, _FakeClock())
    event = _make_event(
        kind="doorbell_pressed",
        entity_id="doorbell.front",
    )
    assert throttle.is_input_kind(event) is True
