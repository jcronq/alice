"""Async experiment dispatch for thinking-side wakes.

The ``run_experiment`` MCP tool (in :mod:`alice_thinking.tools.run_experiment`)
hands a hypothesis + method to an :class:`ExperimentRunner`, which spawns a
sandboxed ``claude`` CLI subagent in an asyncio task and returns dispatch
metadata immediately. The subagent writes a research-paper-shaped markdown
card to ``cortex-memory/experiments/<id>.md`` via its ``submit_result`` MCP
tool. On completion the runner emits a surface note (so thinking sees the
result on her next wake), a ``experiment-card`` event on the viewer event
channel, and appends a line to ``inner/state/experiments.jsonl``.

Design: ``inner/notes/2026-05-11-115827-design-proposal.md`` (v2 async).
Review: ``cortex-memory/research/2026-05-11-run-experiment-tool-design-review.md``.
"""

from __future__ import annotations

from .card import (
    CardContent,
    write_card,
    write_failed_stub_card,
)
from .permissions import generate_permission_rules
from .runner import (
    DEFAULT_TIMEOUT_SECONDS,
    DispatchMetadata,
    ExperimentDispatchError,
    ExperimentRunner,
    new_experiment_id,
)
from .surface import (
    append_experiments_jsonl,
    emit_completion_event,
    write_surface_note,
)


__all__ = [
    "CardContent",
    "DEFAULT_TIMEOUT_SECONDS",
    "DispatchMetadata",
    "ExperimentDispatchError",
    "ExperimentRunner",
    "append_experiments_jsonl",
    "emit_completion_event",
    "generate_permission_rules",
    "new_experiment_id",
    "write_card",
    "write_failed_stub_card",
    "write_surface_note",
]
