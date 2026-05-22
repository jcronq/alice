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
from alice_forge.sm.comments import parse_comment, ParsedVerb, ParseError
from alice_forge.sm.comments import Continue as ParsedContinue
from alice_forge.sm.verify import verify_state_machine, StateMachineInvariantError
from alice_forge.sm.result import (
    BlockedByTTL,
    Continue,
    EmitParseError,
    HandlerResult,
    NoProgress,
    SideEffect,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.enforcement import (
    ENFORCEMENT_ENV_VAR,
    GRACE_TRANSITION_BODY,
    GRACE_TRANSITION_SIDE_EFFECT,
    STRIKE_1_TEMPLATE,
    STRIKE_2_TEMPLATE,
    STRIKE_3_AUDIT_TEMPLATE,
    STRIKE_LIMIT,
    STRIKES_SIDE_EFFECT,
    StrikeAction,
    apply_enforcement,
    clear_strikes,
    compute_grace_block,
    grace_pass_over_issues,
    is_duplicate_continue,
    is_enforcement_enabled,
    record_continue,
)

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
    "ParsedContinue",
    "verify_state_machine",
    "StateMachineInvariantError",
    # HandlerResult variants
    "HandlerResult",
    "Transition",
    "Continue",
    "SideEffect",
    "NoProgress",
    "BlockedByTTL",
    "EmitParseError",
    "HandlerServices",
    # Phase 3: continue-verb enforcement
    "ENFORCEMENT_ENV_VAR",
    "GRACE_TRANSITION_BODY",
    "GRACE_TRANSITION_SIDE_EFFECT",
    "STRIKE_1_TEMPLATE",
    "STRIKE_2_TEMPLATE",
    "STRIKE_3_AUDIT_TEMPLATE",
    "STRIKE_LIMIT",
    "STRIKES_SIDE_EFFECT",
    "StrikeAction",
    "apply_enforcement",
    "clear_strikes",
    "compute_grace_block",
    "grace_pass_over_issues",
    "is_duplicate_continue",
    "is_enforcement_enabled",
    "record_continue",
]
