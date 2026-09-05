"""Tests for :mod:`alice_speaking.thalamus.deep_work` and
:mod:`alice_speaking.thalamus.consumer`.

Split into three layers:

- **Pure helper tests** exercise :func:`is_deep_work_condition` /
  :func:`is_critical` without state-file I/O.
- **FSM tests** drive :func:`route_event` with an in-tmp state file
  through every transition (NORMAL → DEEP_WORK → first-violation
  timer → EXIT_WAITING → re-entry → NORMAL).
- **Consumer tests** confirm the orchestration wrapper mirrors the
  FSM's decisions and sequences batches correctly.

Constants are referenced by name (never magic numbers) so a bump
to :data:`EXIT_TIMEOUT_SECONDS` or :data:`WORK_HOURS` doesn't
silently break assertions.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from alice_speaking.thalamus import consumer, deep_work
from alice_speaking.thalamus.deep_work import (
    ACTION_BUFFER,
    ACTION_NORMAL,
    ACTION_SURFACE_FLASH,
    ENTITY_OFFICE_MOTION,
    ENTITY_OFFICE_QUIET,
    EXIT_TIMEOUT_SECONDS,
    STATE_DEEP_WORK,
    STATE_EXIT_WAITING,
    STATE_NORMAL,
    WORK_HOURS,
)


UTC = datetime.timezone.utc

#: Fixed offset used for tests that assert on WORK_HOURS. Deep-work
#: gates on Jason's *local* hour; without a fixed offset the tests
#: would go red on containers running with a UTC clock. -04:00
#: matches Eastern Daylight for the 2026-09-04 dates the tests use;
#: the exact offset doesn't matter as long as it's consistent.
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-4))


def _entity_state(quiet: str = "off", motion: str = "off"):
    """Return a stub matching the :class:`EntityStateFn` shape."""
    values = {ENTITY_OFFICE_QUIET: quiet, ENTITY_OFFICE_MOTION: motion}
    return values.__getitem__


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a fresh, non-existent state file path under tmp."""
    return tmp_path / "inner" / "thalamus" / "state" / "deep_work.json"


# ---------- pure helpers ----------


def test_is_deep_work_condition_requires_all_three() -> None:
    """All three gates must fire simultaneously. Any one off → False."""
    mid_workday = datetime.datetime(2026, 9, 4, 10, 0)
    both_on = _entity_state(quiet="on", motion="on")
    assert deep_work.is_deep_work_condition(both_on, now=mid_workday) is True
    assert deep_work.is_deep_work_condition(
        _entity_state(quiet="off", motion="on"), now=mid_workday
    ) is False
    assert deep_work.is_deep_work_condition(
        _entity_state(quiet="on", motion="off"), now=mid_workday
    ) is False


def test_is_deep_work_condition_respects_work_hours_boundary() -> None:
    """08:59 and 17:00 are outside the range (:data:`WORK_HOURS`)."""
    both_on = _entity_state(quiet="on", motion="on")
    before = datetime.datetime(2026, 9, 4, 8, 59)
    after = datetime.datetime(2026, 9, 4, 17, 0)
    inside_start = datetime.datetime(2026, 9, 4, WORK_HOURS.start, 0)
    inside_end = datetime.datetime(2026, 9, 4, WORK_HOURS.stop - 1, 59)
    assert deep_work.is_deep_work_condition(both_on, now=before) is False
    assert deep_work.is_deep_work_condition(both_on, now=after) is False
    assert deep_work.is_deep_work_condition(both_on, now=inside_start) is True
    assert deep_work.is_deep_work_condition(both_on, now=inside_end) is True


def test_is_critical_matches_source_kind_and_unknown_person() -> None:
    """Critical detection covers source membership, kind membership,
    and the unknown_person data flag; a plain motion event is not."""
    assert deep_work.is_critical({"source": "doorbell"}) is True
    assert deep_work.is_critical({"kind": "smoke_detected"}) is True
    assert deep_work.is_critical({"type": "co_detected"}) is True
    assert deep_work.is_critical(
        {"source": "camera", "kind": "doorbell", "data": {"unknown_person": True}}
    ) is True
    assert deep_work.is_critical({"source": "motion_sensor"}) is False
    assert deep_work.is_critical({"kind": "motion"}) is False
    assert deep_work.is_critical({}) is False
    # Malformed input doesn't crash.
    assert deep_work.is_critical("not-a-dict") is False  # type: ignore[arg-type]


# ---------- state file I/O ----------


def test_read_state_missing_file_returns_default(state_path: pathlib.Path) -> None:
    """A never-written state path → default NORMAL state, no crash."""
    state = deep_work.read_state(state_path)
    assert state["state"] == STATE_NORMAL
    assert state["session_id"] is None
    assert state["buffered_count"] == 0


def test_read_state_corrupt_json_returns_default(state_path: pathlib.Path) -> None:
    """Invalid JSON in the state file → NORMAL fallback (spec §7:
    corruption never crashes the consumer)."""
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{ not valid json", encoding="utf-8")
    state = deep_work.read_state(state_path)
    assert state["state"] == STATE_NORMAL


def test_read_state_non_dict_root_returns_default(state_path: pathlib.Path) -> None:
    """A JSON list at the root → default. Defensive against manual
    edits that swap out the schema."""
    state_path.parent.mkdir(parents=True)
    state_path.write_text('["not", "an", "object"]', encoding="utf-8")
    assert deep_work.read_state(state_path)["state"] == STATE_NORMAL


def test_write_state_is_atomic_and_readable(state_path: pathlib.Path) -> None:
    """tmp file is cleaned up; write is atomic (no lingering .tmp)."""
    payload = {
        "state": STATE_DEEP_WORK,
        "session_id": "sess-1",
        "entered_at": "2026-09-04T10:00:00+00:00",
        "exit_timer_started_at": None,
        "buffered_count": 3,
    }
    deep_work.write_state(payload, state_path)
    assert state_path.exists()
    assert not state_path.with_suffix(state_path.suffix + ".tmp").exists()
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk == payload


# ---------- FSM: NORMAL → DEEP_WORK ----------


def test_enter_deep_work_writes_session_id_and_entered_at(
    state_path: pathlib.Path,
) -> None:
    """From NORMAL with all conditions met, the FSM transitions to
    DEEP_WORK, mints a session_id, and stamps entered_at."""
    now = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    action = deep_work.route_event(
        {"source": "motion_sensor", "kind": "motion"},
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=now,
    )
    # Triggering event routes normally — entry doesn't retroactively
    # suppress it.
    assert action == ACTION_NORMAL
    state = deep_work.read_state(state_path)
    assert state["state"] == STATE_DEEP_WORK
    assert state["session_id"] is not None
    assert state["entered_at"] is not None
    assert state["exit_timer_started_at"] is None


def test_normal_no_conditions_stays_normal(state_path: pathlib.Path) -> None:
    """NORMAL + conditions off → still NORMAL, no session minted, event
    routes normally."""
    now = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    action = deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="off", motion="off"),
        state_file=state_path,
        now=now,
    )
    assert action == ACTION_NORMAL
    # No session started, so state file may not exist — but if it
    # does, session_id is still None.
    if state_path.exists():
        assert deep_work.read_state(state_path)["session_id"] is None


# ---------- FSM: inside DEEP_WORK ----------


def _enter_deep_work(state_path: pathlib.Path, now: datetime.datetime) -> None:
    """Helper: seed the state file with an active DEEP_WORK session."""
    deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=now,
    )
    assert deep_work.read_state(state_path)["state"] == STATE_DEEP_WORK


def test_non_critical_event_in_deep_work_buffers(
    state_path: pathlib.Path,
) -> None:
    """Kitchen motion during deep work → buffer, not surface."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    later = entry + datetime.timedelta(minutes=5)
    action = deep_work.route_event(
        {"source": "motion_sensor", "kind": "motion", "room": "kitchen"},
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=later,
    )
    assert action == ACTION_BUFFER
    assert deep_work.read_state(state_path)["buffered_count"] == 1


def test_critical_event_in_deep_work_flashes(state_path: pathlib.Path) -> None:
    """Doorbell + unknown_person during deep work → surface_flash."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    action = deep_work.route_event(
        {"source": "doorbell", "kind": "doorbell", "data": {"unknown_person": True}},
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=entry + datetime.timedelta(minutes=5),
    )
    assert action == ACTION_SURFACE_FLASH
    # Critical events are NOT counted against the buffer.
    assert deep_work.read_state(state_path)["buffered_count"] == 0


def test_buffered_count_increments_per_non_critical_event(
    state_path: pathlib.Path,
) -> None:
    """Every non-critical event bumps buffered_count by 1."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    for i in range(1, 4):
        deep_work.route_event(
            {"source": "motion_sensor", "kind": "motion", "seq": i},
            _entity_state(quiet="on", motion="on"),
            state_file=state_path,
            now=entry + datetime.timedelta(minutes=i),
        )
    assert deep_work.read_state(state_path)["buffered_count"] == 3


# ---------- FSM: exit timer ----------


def test_exit_timer_starts_on_first_violation(state_path: pathlib.Path) -> None:
    """DEEP_WORK + conditions failed for the first time → timer set,
    event routes normally, state stays DEEP_WORK."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    violation = entry + datetime.timedelta(minutes=1)
    action = deep_work.route_event(
        {"kind": "motion", "room": "kitchen"},
        _entity_state(quiet="off", motion="on"),
        state_file=state_path,
        now=violation,
    )
    assert action == ACTION_NORMAL
    state = deep_work.read_state(state_path)
    assert state["state"] == STATE_DEEP_WORK
    assert state["exit_timer_started_at"] is not None


def test_exit_timer_expired_transitions_to_exit_waiting(
    state_path: pathlib.Path,
) -> None:
    """DEEP_WORK + violation timer ≥ EXIT_TIMEOUT_SECONDS → EXIT_WAITING."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    # First violation arms the timer.
    first_violation = entry + datetime.timedelta(minutes=1)
    deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="off", motion="on"),
        state_file=state_path,
        now=first_violation,
    )
    # Second event past the 30-min budget — timer expires.
    expired = first_violation + datetime.timedelta(seconds=EXIT_TIMEOUT_SECONDS + 1)
    action = deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="off", motion="on"),
        state_file=state_path,
        now=expired,
    )
    assert action == ACTION_NORMAL
    assert deep_work.read_state(state_path)["state"] == STATE_EXIT_WAITING


def test_exit_timer_running_buffers_non_critical_events(
    state_path: pathlib.Path,
) -> None:
    """Between first violation and 30-min expiry, non-critical events
    buffer (Jason may still return)."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    # Arm the timer.
    first_violation = entry + datetime.timedelta(minutes=1)
    deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="off", motion="on"),
        state_file=state_path,
        now=first_violation,
    )
    # 10 min into the exit window, well before expiry.
    mid_window = first_violation + datetime.timedelta(minutes=10)
    action = deep_work.route_event(
        {"source": "motion_sensor", "kind": "motion"},
        _entity_state(quiet="off", motion="on"),
        state_file=state_path,
        now=mid_window,
    )
    assert action == ACTION_BUFFER


def test_re_enter_clears_exit_timer(state_path: pathlib.Path) -> None:
    """DEEP_WORK + timer running + conditions restored → timer cleared,
    subsequent events buffer as if the violation never happened."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    _enter_deep_work(state_path, entry)
    # Arm the timer.
    deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="off", motion="on"),
        state_file=state_path,
        now=entry + datetime.timedelta(minutes=1),
    )
    assert deep_work.read_state(state_path)["exit_timer_started_at"] is not None
    # Conditions restored — timer clears.
    deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=entry + datetime.timedelta(minutes=2),
    )
    state = deep_work.read_state(state_path)
    assert state["state"] == STATE_DEEP_WORK
    assert state["exit_timer_started_at"] is None


# ---------- FSM: EXIT_WAITING ----------


def test_re_enter_during_exit_waiting_transitions_back_to_deep_work(
    state_path: pathlib.Path,
) -> None:
    """EXIT_WAITING + conditions restored → DEEP_WORK (preserves the
    session — no new session_id minted)."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    # Manually seed EXIT_WAITING with an existing session.
    deep_work.write_state(
        {
            "state": STATE_EXIT_WAITING,
            "session_id": "sess-preserved",
            "entered_at": entry.isoformat(),
            "exit_timer_started_at": None,
            "buffered_count": 5,
        },
        state_path,
    )
    action = deep_work.route_event(
        {"source": "motion_sensor", "kind": "motion"},
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=entry + datetime.timedelta(minutes=45),
    )
    assert action == ACTION_BUFFER
    state = deep_work.read_state(state_path)
    assert state["state"] == STATE_DEEP_WORK
    assert state["session_id"] == "sess-preserved"
    # Existing buffer count is preserved and incremented.
    assert state["buffered_count"] == 6


def test_exit_waiting_without_re_entry_routes_normally(
    state_path: pathlib.Path,
) -> None:
    """EXIT_WAITING + conditions still failed → events pass through
    normally so the flush stream doesn't stall."""
    entry = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    deep_work.write_state(
        {
            "state": STATE_EXIT_WAITING,
            "session_id": "sess-flushing",
            "entered_at": entry.isoformat(),
            "exit_timer_started_at": None,
            "buffered_count": 12,
        },
        state_path,
    )
    action = deep_work.route_event(
        {"kind": "motion"},
        _entity_state(quiet="off", motion="off"),
        state_file=state_path,
        now=entry + datetime.timedelta(minutes=45),
    )
    assert action == ACTION_NORMAL
    # State unchanged (flush is downstream's job).
    assert deep_work.read_state(state_path)["state"] == STATE_EXIT_WAITING


# ---------- is_in_deep_work predicate ----------


def test_is_in_deep_work_true_only_when_state_is_deep_work(
    state_path: pathlib.Path,
) -> None:
    """The habit-cue suppression predicate is strict: EXIT_WAITING
    reads as False so post-session cues resume."""
    # Missing file → NORMAL → False.
    assert deep_work.is_in_deep_work(state_path) is False
    deep_work.write_state({"state": STATE_DEEP_WORK}, state_path)
    assert deep_work.is_in_deep_work(state_path) is True
    deep_work.write_state({"state": STATE_EXIT_WAITING}, state_path)
    assert deep_work.is_in_deep_work(state_path) is False


# ---------- consumer wrapper ----------


def test_consumer_decide_mirrors_route_event(state_path: pathlib.Path) -> None:
    """decide() wraps route_event and returns a ConsumerDecision with
    the same action + echoed event."""
    now = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    event = {"source": "motion_sensor", "kind": "motion"}
    decision = consumer.decide(
        event,
        _entity_state(quiet="off", motion="off"),
        state_file=state_path,
        now=now,
    )
    assert isinstance(decision, consumer.ConsumerDecision)
    assert decision.action == ACTION_NORMAL
    assert decision.event is event


def test_consumer_decide_many_preserves_order_and_sees_transitions(
    state_path: pathlib.Path,
) -> None:
    """decide_many() is sequential — an entry transition on event 0
    is visible to events 1..N, which buffer instead of routing normally."""
    now = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=LOCAL_TZ)
    events = [
        {"source": "motion_sensor", "kind": "motion", "seq": i} for i in range(3)
    ]
    decisions = consumer.decide_many(
        events,
        _entity_state(quiet="on", motion="on"),
        state_file=state_path,
        now=now,
    )
    # Event 0 triggers entry — routes normally.
    assert decisions[0].action == ACTION_NORMAL
    # Events 1+ see DEEP_WORK — buffer.
    assert decisions[1].action == ACTION_BUFFER
    assert decisions[2].action == ACTION_BUFFER
    # Order preserved: seq matches input.
    assert [d.event["seq"] for d in decisions] == [0, 1, 2]
