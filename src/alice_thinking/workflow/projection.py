"""Projection — current-state snapshot of thinking.workflow items.

Pure replay over an event iterable. The projection is rebuildable from
the event log at any time; we cache it in ``projection.json`` so wakes
don't pay the replay cost on the hot path.

Atomic write: serialize to ``<path>.tmp`` in the same directory, fsync,
then ``os.replace`` to the final path. A reader that races a writer
sees either the old file or the new file, never a torn write.

Freshness is by ``last_event_id`` comparison, NOT mtime: the log's mtime
bumps on every append, so an mtime check would mark the projection
stale on every wake. The id comparison is the design's stated approach.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from alice_thinking.workflow.events_log import read_last_event_id
from alice_thinking.workflow.schema import (
    EventType,
    State,
    WorkflowEvent,
    WorkflowItem,
    is_legal_transition,
)


__all__ = [
    "Projection",
    "build_from_events",
    "load_projection",
    "save_projection",
    "is_fresh",
]


@dataclass
class Projection:
    """Derived current-state view, replayable from events.jsonl."""

    last_event_id: Optional[int] = None
    items: dict[str, WorkflowItem] = field(default_factory=dict)


def _apply_open(projection: Projection, event: WorkflowEvent) -> None:
    """Apply an ``open`` event — create a new item in ``opened`` state."""
    payload = event.payload or {}
    item = WorkflowItem(
        id=event.item_id,
        state=State.OPENED,
        type=str(payload.get("type", "other")),
        title=str(payload.get("title", "")),
        priority=int(payload.get("priority", 0)),
        opened_at=str(payload.get("opened_at", event.ts)),
        source=dict(payload.get("source") or {}),
        completion_criterion=payload.get("completion_criterion"),
        unblock_when=payload.get("unblock_when"),
        requeue_when=payload.get("requeue_when"),
        output=payload.get("output"),
        notes=list(payload.get("notes") or []),
        wakes_active=int(payload.get("wakes_active", 0)),
        max_wakes=payload.get("max_wakes"),
    )
    projection.items[item.id] = item


def _apply_transition(
    projection: Projection, event: WorkflowEvent, *, forced: bool
) -> None:
    """Apply a ``transition`` (or ``force_transition``) event.

    Raises :class:`ValueError` on an illegal transition when
    ``forced=False``. When ``forced=True``, the legality check is
    bypassed (this is the design's escape hatch — see Decision #1).
    """
    item = projection.items.get(event.item_id)
    if item is None:
        raise ValueError(
            f"transition event {event.event_id} references unknown item {event.item_id!r}"
        )
    if event.to_state is None:
        raise ValueError(
            f"transition event {event.event_id} for {event.item_id!r} has no to_state"
        )
    if not forced:
        if event.from_state is not None and item.state != event.from_state:
            raise ValueError(
                f"transition event {event.event_id} expected item {event.item_id!r} "
                f"in {event.from_state.value!r}, found {item.state.value!r}"
            )
        if not is_legal_transition(item.state, event.to_state):
            raise ValueError(
                f"illegal transition for item {event.item_id!r}: "
                f"{item.state.value} → {event.to_state.value}"
            )
    item.state = event.to_state


def _apply_update(projection: Projection, event: WorkflowEvent) -> None:
    """Apply an ``update`` event — set non-state fields from the payload.

    Silently ignores attempts to mutate ``state`` (state changes go
    through transition / force_transition). Unknown payload keys are
    ignored so a forward-compatible schema addition doesn't break
    replay on older code.
    """
    item = projection.items.get(event.item_id)
    if item is None:
        raise ValueError(
            f"update event {event.event_id} references unknown item {event.item_id!r}"
        )
    payload = event.payload or {}
    mutable_fields = {
        "type",
        "title",
        "priority",
        "source",
        "completion_criterion",
        "unblock_when",
        "requeue_when",
        "output",
        "wakes_active",
        "max_wakes",
    }
    for key, value in payload.items():
        if key not in mutable_fields:
            continue
        if key == "priority":
            item.priority = int(value)
        elif key == "wakes_active":
            item.wakes_active = int(value)
        elif key == "max_wakes":
            item.max_wakes = None if value is None else int(value)
        else:
            setattr(item, key, value)


def _apply_note_append(projection: Projection, event: WorkflowEvent) -> None:
    """Apply a ``note_append`` event — append text to the item's notes list."""
    item = projection.items.get(event.item_id)
    if item is None:
        raise ValueError(
            f"note_append event {event.event_id} references unknown item {event.item_id!r}"
        )
    payload = event.payload or {}
    note = payload.get("note")
    if note is None:
        return
    item.notes.append(str(note))


def build_from_events(events: Iterable[WorkflowEvent]) -> Projection:
    """Replay events in order to produce a :class:`Projection`.

    The events must already be sorted by ``event_id`` (the log's natural
    order). Raises :class:`ValueError` on an illegal ``transition``
    event; ``force_transition`` is always accepted.
    """
    projection = Projection(last_event_id=None, items={})
    for event in events:
        if event.event_type == EventType.OPEN:
            _apply_open(projection, event)
        elif event.event_type == EventType.TRANSITION:
            _apply_transition(projection, event, forced=False)
        elif event.event_type == EventType.FORCE_TRANSITION:
            _apply_transition(projection, event, forced=True)
        elif event.event_type == EventType.UPDATE:
            _apply_update(projection, event)
        elif event.event_type == EventType.NOTE_APPEND:
            _apply_note_append(projection, event)
        else:  # pragma: no cover — defensive; EventType is exhaustive
            raise ValueError(
                f"unknown event_type {event.event_type!r} in event {event.event_id}"
            )
        if projection.last_event_id is None or event.event_id > projection.last_event_id:
            projection.last_event_id = event.event_id
    return projection


def _projection_to_dict(projection: Projection) -> dict[str, Any]:
    """JSON-friendly dict for ``projection.json``."""
    items_out: dict[str, Any] = {}
    for item_id, item in projection.items.items():
        d = asdict(item)
        # asdict serializes the State enum as the enum object; coerce to its value.
        d["state"] = item.state.value
        items_out[item_id] = d
    return {
        "last_event_id": projection.last_event_id,
        "items": items_out,
    }


def _projection_from_dict(data: dict[str, Any]) -> Projection:
    items_in = data.get("items") or {}
    items: dict[str, WorkflowItem] = {}
    for item_id, raw in items_in.items():
        items[item_id] = WorkflowItem(
            id=str(raw["id"]),
            state=State(raw["state"]),
            type=str(raw.get("type", "other")),
            title=str(raw.get("title", "")),
            priority=int(raw.get("priority", 0)),
            opened_at=str(raw.get("opened_at", "")),
            source=dict(raw.get("source") or {}),
            completion_criterion=raw.get("completion_criterion"),
            unblock_when=raw.get("unblock_when"),
            requeue_when=raw.get("requeue_when"),
            output=raw.get("output"),
            notes=list(raw.get("notes") or []),
            wakes_active=int(raw.get("wakes_active", 0)),
            max_wakes=raw.get("max_wakes"),
        )
    last_id = data.get("last_event_id")
    return Projection(
        last_event_id=None if last_id is None else int(last_id),
        items=items,
    )


def load_projection(path: Path) -> Projection:
    """Read the projection snapshot. Returns an empty projection if missing.

    Corrupt JSON is treated as missing — the caller can rebuild from the
    event log. (Tighter handling, e.g. backing up the corrupt file
    before overwriting, is left for a later iteration.)
    """
    if not path.is_file():
        return Projection(last_event_id=None, items={})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Projection(last_event_id=None, items={})
    if not isinstance(data, dict):
        return Projection(last_event_id=None, items={})
    return _projection_from_dict(data)


def save_projection(path: Path, projection: Projection) -> None:
    """Atomically write ``projection`` to ``path``.

    Writes to ``<path>.tmp`` in the same directory (so the rename stays
    on one filesystem), fsync's the data, then ``os.replace``. A reader
    racing a writer sees either the old or new file, never a torn write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                _projection_to_dict(projection), fh, indent=2, sort_keys=True
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def is_fresh(projection: Projection, events_log_path: Path) -> bool:
    """True iff the projection's last_event_id matches the log's tail.

    Both None (empty log + empty projection) counts as fresh.
    """
    log_last = read_last_event_id(events_log_path)
    return projection.last_event_id == log_last
