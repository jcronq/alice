"""SM v3 — closed state machine, useful-comment-per-iteration, unified ledger.

Design doc: ``inner/designs/2026-05-21-sm-v3-design.md`` (alice-mind vault).
Tracking issue: jcronq/alice#301.

This is the Phase 0 spike — core types only, no handler logic yet. The
v1 dispatcher under ``alice_forge.dispatcher`` continues to run
production traffic until ports land in subsequent phases.

Public surface:

  * :class:`SMState` — typed enum for the twelve ``sm:*`` labels.
  * :class:`StateMeta` — per-state metadata (terminal? default TTL?).
  * :class:`Verbs` — typed enum for ``[SM] <verb>`` comment verbs.
  * :data:`TRANSITIONS` — declarative table of legal transitions.
  * :func:`verify_state_machine` — invariant check, runs at startup + CI.
  * :class:`EmittedLedger` — unified side-effect tracking with TTL +
    completion-contract semantics.
  * :func:`parse_comment` — loud-failure parser that emits a
    ``ParseError`` (with a reply body) on malformed ``[SM] ``
    comments rather than logging silently.
"""

from __future__ import annotations

from alice_forge.sm.states import SMState, StateMeta, STATE_META
from alice_forge.sm.transitions import (
    TRANSITIONS,
    Verbs,
    TransitionTo,
    SelfLoop,
    EventTransition,
)
from alice_forge.sm.ledger import EmittedRecord, EmittedLedger
from alice_forge.sm.comments import parse_comment, ParsedVerb, ParseError, Continue
from alice_forge.sm.verify import verify_state_machine, StateMachineInvariantError

__all__ = [
    "SMState",
    "StateMeta",
    "STATE_META",
    "TRANSITIONS",
    "Verbs",
    "TransitionTo",
    "SelfLoop",
    "EventTransition",
    "EmittedRecord",
    "EmittedLedger",
    "parse_comment",
    "ParsedVerb",
    "ParseError",
    "Continue",
    "verify_state_machine",
    "StateMachineInvariantError",
]
