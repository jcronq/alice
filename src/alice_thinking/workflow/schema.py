"""Typed dataclasses + enums for the thinking.workflow state machine.

States, event types, the legal-transition table, and JSON serialization
helpers for :class:`WorkflowEvent`. Pure data; no I/O.

The state machine is documented in
``alice-mind/inner/designs/2026-05-20-thinking-workflow-design.md``
under "Proposed state machine" — the table here is the authoritative
encoding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


__all__ = [
    "State",
    "EventType",
    "WorkflowItem",
    "WorkflowEvent",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "is_legal_transition",
    "event_to_dict",
    "event_from_dict",
]


class State(str, Enum):
    """Workflow item lifecycle states."""

    OPENED = "opened"
    ACTIVE = "active"
    BLOCKED = "blocked"
    SHELVED = "shelved"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class EventType(str, Enum):
    """Event catalog from the "every-change-is-an-event" decision."""

    OPEN = "open"
    TRANSITION = "transition"
    UPDATE = "update"
    FORCE_TRANSITION = "force_transition"
    NOTE_APPEND = "note_append"


# Legal transitions. Source of truth for the harness validator.
# completed and abandoned are terminal — no outbound edges.
LEGAL_TRANSITIONS: frozenset[tuple[State, State]] = frozenset(
    {
        (State.OPENED, State.ACTIVE),
        (State.ACTIVE, State.BLOCKED),
        (State.ACTIVE, State.SHELVED),
        (State.ACTIVE, State.COMPLETED),
        (State.ACTIVE, State.ABANDONED),
        (State.BLOCKED, State.OPENED),
        (State.BLOCKED, State.ABANDONED),
        (State.SHELVED, State.OPENED),
        (State.SHELVED, State.ABANDONED),
    }
)


TERMINAL_STATES: frozenset[State] = frozenset({State.COMPLETED, State.ABANDONED})


def is_legal_transition(from_state: State, to_state: State) -> bool:
    """Return True if ``from_state → to_state`` is in the legal table."""
    return (from_state, to_state) in LEGAL_TRANSITIONS


@dataclass
class WorkflowItem:
    """A single tracked work item.

    ``priority`` is an integer: higher value = higher priority. The design
    doc uses the string enum ``critical|high|normal|low`` at the YAML
    schema level; PR 1 ships with an int and PR 2 will reconcile (see the
    PR body for the open decision).

    ``completion_criterion`` / ``unblock_when`` / ``requeue_when`` are
    free-shape dicts at this layer — the harness validators inspect their
    ``type`` field. Schemas: see the design doc's "Item schema" section.
    """

    id: str
    state: State
    type: str
    title: str
    priority: int
    opened_at: str
    source: dict[str, Any]
    completion_criterion: Optional[dict[str, Any]] = None
    unblock_when: Optional[dict[str, Any]] = None
    requeue_when: Optional[dict[str, Any]] = None
    output: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    wakes_active: int = 0
    max_wakes: Optional[int] = None


@dataclass
class WorkflowEvent:
    """One immutable line in the events.jsonl log.

    ``event_id`` is monotonic, assigned by :func:`events_log.append_event`
    under flock. ``ts`` is an ISO-8601 timestamp string (the harness emits
    ``datetime.isoformat()`` directly; we don't impose a TZ at this layer).

    ``from_state`` / ``to_state`` are populated for ``transition`` and
    ``force_transition`` events. ``payload`` carries event-specific
    fields: for ``open`` it carries the item's initial fields; for
    ``update`` it carries the changed fields; for ``note_append`` it
    carries the appended note text.
    """

    event_id: int
    ts: str
    event_type: EventType
    item_id: str
    from_state: Optional[State] = None
    to_state: Optional[State] = None
    by: str = ""
    reason: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    bypassed_validation: bool = False
    payload: Optional[dict[str, Any]] = None


def event_to_dict(event: WorkflowEvent) -> dict[str, Any]:
    """Serialize a :class:`WorkflowEvent` to a JSON-ready dict.

    Enum values are stringified; ``None`` fields are emitted as JSON
    ``null`` so a round-trip preserves shape (rather than dropping keys
    and then defaulting them on read).
    """
    return {
        "event_id": event.event_id,
        "ts": event.ts,
        "event_type": event.event_type.value,
        "item_id": event.item_id,
        "from_state": event.from_state.value if event.from_state is not None else None,
        "to_state": event.to_state.value if event.to_state is not None else None,
        "by": event.by,
        "reason": event.reason,
        "evidence": event.evidence,
        "bypassed_validation": event.bypassed_validation,
        "payload": event.payload,
    }


def event_from_dict(data: dict[str, Any]) -> WorkflowEvent:
    """Deserialize a JSON dict back to :class:`WorkflowEvent`.

    Tolerates missing optional keys (older log lines that predate a
    schema addition come back with the default). Required fields:
    ``event_id``, ``ts``, ``event_type``, ``item_id``.
    """
    from_state_raw = data.get("from_state")
    to_state_raw = data.get("to_state")
    return WorkflowEvent(
        event_id=int(data["event_id"]),
        ts=str(data["ts"]),
        event_type=EventType(data["event_type"]),
        item_id=str(data["item_id"]),
        from_state=State(from_state_raw) if from_state_raw is not None else None,
        to_state=State(to_state_raw) if to_state_raw is not None else None,
        by=str(data.get("by", "")),
        reason=data.get("reason"),
        evidence=data.get("evidence"),
        bypassed_validation=bool(data.get("bypassed_validation", False)),
        payload=data.get("payload"),
    )
