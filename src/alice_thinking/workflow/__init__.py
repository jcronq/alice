"""thinking.workflow — deterministic state machine harness for thinking's work queue.

PR 1 of 4 in the thinking.workflow migration. Pure harness module: events.jsonl
append-only log + projection.json snapshot, driven by deterministic Python (not
the LLM). No integration with the wake template, github.workflow bridge, or
backfill flow — those land in subsequent PRs.

Design: ``alice-mind/inner/designs/2026-05-20-thinking-workflow-design.md``.
"""

from __future__ import annotations

from alice_thinking.workflow.events_log import (
    append_event,
    read_events,
    read_last_event_id,
)
from alice_thinking.workflow.harness import Harness
from alice_thinking.workflow.projection import (
    Projection,
    build_from_events,
    is_fresh,
    load_projection,
    save_projection,
)
from alice_thinking.workflow.schema import (
    EventType,
    State,
    WorkflowEvent,
    WorkflowItem,
    event_from_dict,
    event_to_dict,
    is_legal_transition,
)

__all__ = [
    "EventType",
    "Harness",
    "Projection",
    "State",
    "WorkflowEvent",
    "WorkflowItem",
    "append_event",
    "build_from_events",
    "event_from_dict",
    "event_to_dict",
    "is_fresh",
    "is_legal_transition",
    "load_projection",
    "read_events",
    "read_last_event_id",
    "save_projection",
]
