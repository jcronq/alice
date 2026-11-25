"""Agent UI SSE event envelopes (Phase 2 backend port).

Pure builders for the event types that ride the existing
``/api/events`` SSE stream alongside the Phase 1 ``section.update``
and ``page:<slug>`` events. Each helper returns the dict shape
expected by the frontend decoder; the handler layer calls
``cfg.broadcaster.publish_event(ev)`` to fan one out.

Shapes mirror ``reference/agent-ui-testing/shared/src/events.ts``
(lines 64-76). The on-wire ``type`` is the discriminant; the payload
fields match 1:1 with the TS ``ServerEvent`` union.

Backward compatibility invariant: these are STRICTLY ADDITIVE.
Nothing here replaces or renames an existing event — the Phase 1
``index.html`` renderer keeps consuming ``section.update`` /
``page:<slug>`` unchanged.

Stdlib-only (same constraint as the rest of the module).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# Agent UI's ``AgentStatus`` union (see events.ts line 6). The
# Phase 2 backend emits a tiny subset; the frontend accepts arbitrary
# strings and renders ``idle`` fallbacks for anything it doesn't know.
AGENT_STATUS_IDLE = "idle"
AGENT_STATUS_WORKING = "working"
AGENT_STATUS_READY = "ready"
AGENT_STATUS_DOWN = "down"


def snapshot_fields(
    todos: Sequence[Mapping[str, Any]],
    *,
    work_statuses: Sequence[Mapping[str, Any]] | None = None,
    agent_status: str = AGENT_STATUS_IDLE,
) -> dict:
    """Build the fields the Agent UI snapshot carries.

    Returned dict merges INTO the existing ``type: snapshot`` payload
    (``SectionBroadcaster.snapshot``) rather than replacing it, so a
    Phase 1 client reading ``sections`` stays happy and a Phase 2
    client reads ``todos`` / ``workStatuses`` / ``agentStatus`` off
    the same event.
    """
    return {
        "todos": list(todos),
        "workStatuses": list(work_statuses or []),
        "agentStatus": agent_status,
    }


def todo_upsert_event(todo: Mapping[str, Any]) -> dict:
    """``todo.upsert`` — fired on any write that changes a Todo.

    Agent UI's TS event carries the field as ``item`` (events.ts
    line 69); we emit it there AND as ``todo`` for the P2 frontend
    the task brief references (``{todo: Todo}``). Frontends that read
    either key stay happy.
    """
    return {"type": "todo.upsert", "item": dict(todo), "todo": dict(todo)}


def todo_removed_event(todo_id: str) -> dict:
    """``todo.removed`` — fired on resolve / dismiss / delete."""
    return {"type": "todo.removed", "id": str(todo_id)}


def thread_message_event(
    todo_id: str, message: Mapping[str, Any]
) -> dict:
    """``thread.message`` — appended one new message to a Todo's thread.

    Carries ``thread`` as the Agent UI ``ThreadRef`` (``{kind: "todo",
    id}``) AND the Phase 2 brief's convenience ``todoId`` field.
    """
    return {
        "type": "thread.message",
        "thread": {"kind": "todo", "id": str(todo_id)},
        "todoId": str(todo_id),
        "message": dict(message),
    }


def thread_delta_event(
    todo_id: str, delta: str, message_id: str, *, index: int = 0
) -> dict:
    """``thread.delta`` — one chunk of a streaming assistant reply.

    Carries ``index`` so a client can stitch deltas in order even when
    they arrive out of band. ``streamId`` mirrors ``messageId`` for
    the TS shape (``{streamId, messageId, index, text}``); the P2
    brief names the chunk field ``delta`` so we emit both keys.
    """
    return {
        "type": "thread.delta",
        "thread": {"kind": "todo", "id": str(todo_id)},
        "todoId": str(todo_id),
        "streamId": str(message_id),
        "messageId": str(message_id),
        "index": int(index),
        "text": str(delta),
        "delta": str(delta),
    }


def work_update_event(work_statuses: Sequence[Mapping[str, Any]]) -> dict:
    """``work.update`` — the status-line entries changed.

    The TS shape names the field ``items`` (events.ts line 67); the
    P2 brief names it ``workStatuses``. Emit both.
    """
    rows = [dict(r) for r in work_statuses]
    return {"type": "work.update", "items": rows, "workStatuses": rows}


def agent_status_event(agent_status: str) -> dict:
    """``agent.status`` — the agent's top-level state changed."""
    return {
        "type": "agent.status",
        "state": str(agent_status),
        "agentStatus": str(agent_status),
    }
