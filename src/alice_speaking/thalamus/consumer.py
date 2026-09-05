"""Speaking-side thalamus consumer — deep-work-aware routing.

Sits downstream of the memory-worker's Stage B thalamus drain
(``alice_thinking.memory_worker.thalamus``) and upstream of the
``inner/surface/`` promoter and ``inner/notes/`` appender. Applies
the deep-work FSM to every filtered event and returns a routing
decision the caller then materializes as a filesystem effect.

Pure decision layer — :func:`decide` is a thin wrapper over
:func:`alice_speaking.thalamus.deep_work.route_event` so tests
exercise the decision path without fixtures. Batch variant
:func:`decide_many` preserves ordering so state transitions
triggered mid-batch are visible to later events.

Design contract — see:

- ``cortex-memory/research/2026-08-07-deep-work-implementation-spec.md``
  §8 (integration point; the sketch predates the Stage B / speaking
  split so the actual scan-and-route loop landed here rather than
  inside the memory-worker's ``stage_b()`` method).
- ``cortex-memory/research/2026-08-07-deep-work-buffer-integration.md``
  §5 (SSE processing order).

Separation of concerns:

- **This module** — pure decision. Given an event dict, returns the
  action name from :mod:`.deep_work`. No filesystem writes beyond
  the state-file rewrite triggered by an FSM transition (owned by
  :mod:`.deep_work`, not this module).
- **Downstream promoters** — surface watcher, note appender, buffer
  writer. Each consumes a :class:`ConsumerDecision` and materializes
  the effect appropriate to its output directory. Kept separate so a
  buffer-write failure doesn't stall the surface-write path.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import pathlib
from typing import Any, Callable, Optional

from . import deep_work


logger = logging.getLogger(__name__)


#: Same type alias as :data:`deep_work.EntityStateFn` — re-exported
#: here so consumer callers don't have to import both modules.
EntityStateFn = Callable[[str], Optional[str]]


@dataclasses.dataclass
class ConsumerDecision:
    """One routing decision produced by :func:`decide`.

    ``action`` is one of the ``ACTION_*`` constants defined on
    :mod:`.deep_work`. ``event`` echoes the input dict so downstream
    filesystem effects don't have to re-parse.
    """

    action: str
    event: dict[str, Any]


def decide(
    event: dict[str, Any],
    entity_state_fn: EntityStateFn,
    *,
    state_file: pathlib.Path = deep_work.DEFAULT_STATE_FILE,
    now: Optional[datetime.datetime] = None,
) -> ConsumerDecision:
    """Apply the deep-work FSM to one event. Return the decision.

    Thin orchestration wrapper over
    :func:`deep_work.route_event`. Any filesystem effect
    (promotion to ``inner/surface/``, buffer append, drop) is the
    caller's job — this keeps ``decide()`` unit-testable without
    fixture scaffolding.
    """
    action = deep_work.route_event(
        event,
        entity_state_fn,
        state_file=state_file,
        now=now,
    )
    return ConsumerDecision(action=action, event=event)


def decide_many(
    events: list[dict[str, Any]],
    entity_state_fn: EntityStateFn,
    *,
    state_file: pathlib.Path = deep_work.DEFAULT_STATE_FILE,
    now: Optional[datetime.datetime] = None,
) -> list[ConsumerDecision]:
    """Batch variant — sequentially route ``events``.

    Sequential routing (not parallel) means an FSM transition
    triggered by event N is visible to events N+1..M. Matches the
    Stage B ordering guarantee — intake files are processed in
    sorted-name order so batches are deterministic on replay.
    """
    return [
        decide(ev, entity_state_fn, state_file=state_file, now=now)
        for ev in events
    ]
