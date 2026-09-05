"""Deep-work protection state machine for the speaking-side thalamus.

Applies deep-work context to events that survived the memory
worker's Stage B routing (see
``alice_thinking.memory_worker.thalamus``). Decides whether each
event should surface, buffer for post-session review, flash-surface
(critical), drop, or route normally, based on Jason's deep-work
state.

State machine (design in
``cortex-memory/research/2026-08-07-deep-work-state-machine-design.md``)::

    NORMAL ── office_quiet=ON ∧ office_motion=ON ∧ 09:00-17:00 ──> DEEP_WORK
    DEEP_WORK ── conditions fail for >= 30 min ─────────────────> EXIT_WAITING
    EXIT_WAITING ── buffer flush complete ──────────────────────> NORMAL
    EXIT_WAITING ── conditions restored inside 30-min window ───> DEEP_WORK

State persists to
``~/alice-mind/inner/thalamus/state/deep_work.json`` (atomic write —
tmp file + rename, same discipline as the Stage B decision writes).
Missing/corrupt state file always falls back to :data:`STATE_NORMAL`
so a bad state doc can't wedge the consumer.

Decisions in this module are **pure** — no filesystem side effects
beyond the state-file rewrite triggered by a transition. The buffer
append / promotion / flush effects belong to
:mod:`alice_speaking.thalamus.consumer` and downstream promoters.

Design docs:

- ``cortex-memory/research/2026-08-07-deep-work-state-machine-design.md``
- ``cortex-memory/research/2026-08-07-deep-work-buffer-integration.md``
- ``cortex-memory/research/2026-08-07-deep-work-implementation-spec.md``
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import uuid
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


# ---------- module constants (see spec § 8) ----------

#: Default location of the deep-work FSM state document. Tests
#: override with ``state_file=tmp_path/.../deep_work.json``.
DEFAULT_STATE_FILE = pathlib.Path(
    "/home/alice/alice-mind/inner/thalamus/state/deep_work.json"
)

#: Root of the per-session buffer tree
#: (``buffer/<session_id>/{manifest.json,events.jsonl}``).
#: Managed by :mod:`alice_speaking.thalamus.consumer` — this module
#: only tracks buffered_count in state; it does not touch the tree.
DEFAULT_BUFFER_DIR = pathlib.Path(
    "/home/alice/alice-mind/inner/thalamus/buffer"
)

#: Continuous seconds of failed deep-work conditions before the FSM
#: transitions ``DEEP_WORK → EXIT_WAITING``. Long enough that a
#: bathroom / kitchen refill doesn't kill the session; short enough
#: that a genuine session end publishes within the same hour.
EXIT_TIMEOUT_SECONDS = 30 * 60

#: Local wall-clock hour range during which deep-work entry is
#: possible. Outside this window, entry conditions are never met —
#: an evening office session routes normally.
WORK_HOURS = range(9, 17)  # 09:00 through 16:59 local

# State string constants — match the on-disk wire format so tools
# that read ``deep_work.json`` directly can compare with plain str
# equality without importing this module.
STATE_NORMAL = "normal"
STATE_DEEP_WORK = "deep_work"
STATE_EXIT_WAITING = "exit_waiting"

#: CozyHem entity ID whose ``on`` value expresses "Jason wants
#: quiet". Toggled by a physical office switch + a schedule
#: automation; either can flip it.
ENTITY_OFFICE_QUIET = "input_boolean.office_quiet"

#: CozyHem motion sensor in the office. ``on`` while Jason is
#: physically present (with a ~5 min timeout the sensor firmware
#: handles).
ENTITY_OFFICE_MOTION = "binary_sensor.office_motion"

# Actions returned by :func:`route_event`. Callers switch on these
# to perform the filesystem side effect (promotion / buffer / flash /
# drop). Kept as bare strings to match the spec's wire vocabulary.
ACTION_SURFACE = "surface"
ACTION_BUFFER = "buffer"
ACTION_SURFACE_FLASH = "surface_flash"
ACTION_DROPPED = "dropped"
ACTION_NORMAL = "normal"

#: Event ``source`` values that always interrupt deep work. Aligns
#: with the memory-worker's :data:`FAST_PATH_KINDS` — those kinds
#: are dropped for volume audit; the sources here are the ones
#: whose ambient (non-fast-path) observations should still flash.
CRITICAL_SOURCES = frozenset(
    {"doorbell", "smoke_detector", "glass_break", "co_detector", "leak_detector"}
)

#: Event ``kind`` values that always interrupt deep work. Belt +
#: suspenders vs. :data:`CRITICAL_SOURCES` so an event whose source
#: was normalized but whose kind carries the critical signal is
#: still surfaced.
CRITICAL_KINDS = frozenset(
    {
        "smoke_detected",
        "glass_break",
        "doorbell_pressed",
        "co_detected",
        "leak_detected",
    }
)


#: Type alias for the callable a caller passes so :func:`route_event`
#: can inspect CozyHem entity state. ``fn(entity_id) -> "on" | "off"
#: | <value> | None``. Kept as a plain Callable so tests can pass a
#: dict.__getitem__-style stub without wrapping.
EntityStateFn = Callable[[str], Optional[str]]


# ---------- state file I/O ----------


def _empty_state() -> dict[str, Any]:
    """The default NORMAL-state document. Also used as a merge base
    so a partially-populated file (from an older writer / manual
    edit) still exposes every key downstream code expects.
    """
    return {
        "state": STATE_NORMAL,
        "session_id": None,
        "entered_at": None,
        "exit_timer_started_at": None,
        "buffered_count": 0,
    }


def read_state(state_file: pathlib.Path = DEFAULT_STATE_FILE) -> dict[str, Any]:
    """Read the state document. Missing / corrupt / non-dict → NORMAL.

    A corrupt state file must never crash the consumer. The safe
    fallback is NORMAL routing (every event passes through
    normally). The next :func:`write_state` call overwrites the
    broken file with a clean document.
    """
    try:
        raw = state_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _empty_state()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "deep_work: state file %s is corrupt JSON — defaulting to NORMAL",
            state_file,
        )
        return _empty_state()
    if not isinstance(parsed, dict):
        logger.warning(
            "deep_work: state file %s is not an object — defaulting to NORMAL",
            state_file,
        )
        return _empty_state()
    merged = _empty_state()
    merged.update({k: v for k, v in parsed.items() if k in merged})
    return merged


def write_state(
    state: dict[str, Any],
    state_file: pathlib.Path = DEFAULT_STATE_FILE,
) -> None:
    """Atomic write — tmp file + :func:`os.replace`.

    Partial writes leave the previous state file intact, so a
    crashed consumer never leaves the state doc half-written. Same
    discipline as the Stage B thalamus decision writer.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, state_file)


# ---------- pure helpers ----------


def _utcnow() -> datetime.datetime:
    """Timezone-aware "now" — extracted so tests can freeze time."""
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _parse_iso(raw: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 string back to a tz-aware datetime.

    Naive strings are coerced to UTC — the only writer here is
    :func:`route_event` which always writes UTC, so this covers the
    manual-edit / older-writer case safely.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def is_deep_work_condition(
    entity_state_fn: EntityStateFn,
    *,
    now: Optional[datetime.datetime] = None,
) -> bool:
    """All three deep-work entry conditions met simultaneously?

    - ``input_boolean.office_quiet`` == ``"on"``
    - ``binary_sensor.office_motion`` == ``"on"``
    - Current hour in :data:`WORK_HOURS` (09:00–16:59 local)

    ``now`` overrides the wall-clock read so tests exercise the
    WORK_HOURS boundary deterministically. Callers pass a *local*
    datetime — the ``hour`` attribute is compared as-is.
    """
    if now is None:
        now = datetime.datetime.now()
    if now.hour not in WORK_HOURS:
        return False
    if entity_state_fn(ENTITY_OFFICE_QUIET) != "on":
        return False
    if entity_state_fn(ENTITY_OFFICE_MOTION) != "on":
        return False
    return True


def is_critical(event: dict[str, Any]) -> bool:
    """Should ``event`` flash-surface even during deep work?

    Accepts a dict with any of ``source`` / ``kind`` / ``type``
    (``type`` is an alias for ``kind`` to match the spec's inline
    example). Also honours a nested ``data.unknown_person`` flag
    for doorbell rings — that's a security concern regardless of
    the source's normal classification.
    """
    if not isinstance(event, dict):
        return False
    source = event.get("source")
    kind = event.get("kind") or event.get("type")
    if isinstance(source, str) and source in CRITICAL_SOURCES:
        return True
    if isinstance(kind, str) and kind in CRITICAL_KINDS:
        return True
    data = event.get("data")
    if isinstance(data, dict) and data.get("unknown_person"):
        # Any unknown_person flagged event escalates (spec §3
        # example lists doorbell specifically, but the same signal
        # from another source would carry the same weight).
        return True
    return False


# ---------- state machine ----------


def route_event(
    event: dict[str, Any],
    entity_state_fn: EntityStateFn,
    *,
    state_file: pathlib.Path = DEFAULT_STATE_FILE,
    now: Optional[datetime.datetime] = None,
) -> str:
    """Route ``event`` through the deep-work FSM. Return an action.

    Returns one of :data:`ACTION_SURFACE`, :data:`ACTION_BUFFER`,
    :data:`ACTION_SURFACE_FLASH`, :data:`ACTION_DROPPED`,
    :data:`ACTION_NORMAL`.

    Side effect: rewrites ``state_file`` when a transition fires:

    - ``NORMAL → DEEP_WORK``: entry conditions met.
    - ``DEEP_WORK`` first-violation: ``exit_timer_started_at`` set.
    - ``DEEP_WORK → EXIT_WAITING``: exit timer elapsed
      (:data:`EXIT_TIMEOUT_SECONDS`).
    - ``EXIT_WAITING → DEEP_WORK``: conditions restored inside the
      exit window.

    ``now`` overrides the clock read (UTC datetime) for tests. The
    UTC value is converted to local for the :data:`WORK_HOURS`
    check via ``now.astimezone()``.
    """
    if now is None:
        now = _utcnow()

    state = read_state(state_file)
    current = state.get("state", STATE_NORMAL)

    conditions_met = is_deep_work_condition(entity_state_fn, now=now.astimezone())

    if current == STATE_NORMAL:
        if conditions_met:
            state.update(
                {
                    "state": STATE_DEEP_WORK,
                    "session_id": str(uuid.uuid4()),
                    "entered_at": _iso(now),
                    "exit_timer_started_at": None,
                    "buffered_count": 0,
                }
            )
            write_state(state, state_file)
        # NORMAL and the transitioning event both route freely —
        # entry conditions can't retroactively suppress the
        # observation that triggered them.
        return ACTION_NORMAL

    if current == STATE_DEEP_WORK:
        if conditions_met:
            # Still deep. Clear any half-started exit timer — Jason
            # came back inside the tick window.
            if state.get("exit_timer_started_at"):
                state["exit_timer_started_at"] = None
                write_state(state, state_file)
            if is_critical(event):
                return ACTION_SURFACE_FLASH
            state["buffered_count"] = int(state.get("buffered_count", 0)) + 1
            write_state(state, state_file)
            return ACTION_BUFFER

        # Conditions failed. Check / arm the exit timer.
        timer_start = _parse_iso(state.get("exit_timer_started_at"))
        if timer_start is None:
            # First violation — arm timer, route this event
            # normally (spec §8 code sketch: "Route normally on
            # first violation").
            state["exit_timer_started_at"] = _iso(now)
            write_state(state, state_file)
            return ACTION_NORMAL
        elapsed = (now - timer_start).total_seconds()
        if elapsed >= EXIT_TIMEOUT_SECONDS:
            # 30-min budget spent — transition to EXIT_WAITING.
            # This event routes normally so the flush stream isn't
            # racing the transition.
            state["state"] = STATE_EXIT_WAITING
            write_state(state, state_file)
            return ACTION_NORMAL
        # Timer running but not yet expired — buffer non-critical
        # events (Jason may still return within the window).
        if is_critical(event):
            return ACTION_SURFACE_FLASH
        state["buffered_count"] = int(state.get("buffered_count", 0)) + 1
        write_state(state, state_file)
        return ACTION_BUFFER

    if current == STATE_EXIT_WAITING:
        if conditions_met:
            # Re-entry inside the exit window — preserve the
            # session (buffered_count carries forward).
            state.update(
                {
                    "state": STATE_DEEP_WORK,
                    "exit_timer_started_at": None,
                }
            )
            write_state(state, state_file)
            if is_critical(event):
                return ACTION_SURFACE_FLASH
            state["buffered_count"] = int(state.get("buffered_count", 0)) + 1
            write_state(state, state_file)
            return ACTION_BUFFER
        # Still waiting for flush to complete — pass events through
        # so the daily / notes appenders see them in real time.
        return ACTION_NORMAL

    # Unknown state — safe recovery: reset to NORMAL and route
    # this event through.
    logger.warning(
        "deep_work: unknown state %r in %s — resetting to NORMAL",
        current,
        state_file,
    )
    write_state(_empty_state(), state_file)
    return ACTION_NORMAL


def is_in_deep_work(state_file: pathlib.Path = DEFAULT_STATE_FILE) -> bool:
    """Predicate for outside callers (habit cue suppression).

    Reads the state file; returns ``True`` iff the current state
    is :data:`STATE_DEEP_WORK`. ``EXIT_WAITING`` returns ``False``
    so post-session ambient cues (kitchen wake, pre-sleep) resume
    immediately after the exit timer expires — matches the
    buffer-integration §6 rule.
    """
    state = read_state(state_file)
    return state.get("state") == STATE_DEEP_WORK
