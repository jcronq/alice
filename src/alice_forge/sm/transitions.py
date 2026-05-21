"""Declarative transition table for SM v3.

This module is the single source of truth for which transitions are
legal from which state. :func:`verify_state_machine` reads
:data:`TRANSITIONS` at dispatcher startup and CI to enforce the I-1
(completeness) and I-2 (closure) invariants. Adding a state means
both a new :class:`SMState` member AND a new key in this table.

Three transition kinds:

  * :class:`TransitionTo` — comment-verb-driven move to another state.
  * :class:`SelfLoop` — explicit ``[SM] continue`` self-loop. Subject
    to the I-4 "useful" check (hash-dedup + three-strike escalation).
  * :class:`EventTransition` — implicit transitions driven by world
    state (linked PR opens, CI flips green, spawn dispatches, etc.).
    These are *not* comment-driven; the dispatcher detects them by
    polling external systems.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping

from alice_forge.sm.states import SMState


class Verbs(enum.Enum):
    """Every ``[SM] <verb>`` the v3 parser accepts.

    String value is the canonical token after ``[SM] `` — the parser
    matches against this exactly (whitespace-tolerant on the trailing
    end via the parser's own logic).
    """

    # sm:draft outgoing
    ROUTE_TO_STUDY = "route-to-study"
    REJECT = "reject"

    # sm:needs_study outgoing
    STUDY_COMPLETE = "study-complete"
    STUDY_BLOCKED = "study-blocked"
    STUDY_REJECTED = "study-rejected"

    # sm:selected outgoing
    RETURN_TO_STUDY = "return-to-study"

    # sm:designing outgoing
    DESIGN_READY = "design-ready"

    # sm:design_review outgoing
    DESIGN_APPROVED = "design-approved"
    DESIGN_REVISE = "design-revise"
    DESIGN_REJECTED = "design-rejected"

    # sm:compacting outgoing
    BUILD_STARTED = "build-started"

    # sm:done outgoing (close-gate only)
    EXIT_TRANSITION = "exit-transition"

    # sm:blocked outgoing
    UNBLOCK = "unblock"

    # Universal self-loop verb (replaces v1's nine per-state verbs)
    CONTINUE = "continue"


@dataclass(frozen=True)
class TransitionTo:
    """Move to ``target`` when this verb parses successfully.

    ``art_swap`` indicates the verb may carry an ``art=<label>`` field
    that swaps the issue's art label during the transition. The v3
    parser surfaces the optional field; the dispatcher applies it.
    """

    target: SMState
    art_swap: bool = False


@dataclass(frozen=True)
class SelfLoop:
    """Self-loop on the universal ``[SM] continue`` verb.

    ``usefulness_check`` is always ``True`` in v3 — the I-4 hash-dedup
    and three-strike escalation always run. The flag exists for
    future experimentation (e.g., a state where strikes shouldn't
    escalate); set ``False`` to opt out at your peril.
    """

    usefulness_check: bool = True


@dataclass(frozen=True)
class EventTransition:
    """Implicit transition driven by world state, not a comment verb.

    ``trigger`` is a free-form identifier (``"linked-pr-open"``,
    ``"pr-merged-and-master-green"``, etc.). The dispatcher's
    per-state handler is responsible for polling the world and
    deciding whether the trigger has fired.
    """

    target: SMState
    trigger: str


# The transition table. Keys: source state. Values: mapping of
# (Verbs | str) → (TransitionTo | SelfLoop | EventTransition). The
# str-keyed entries are EventTransitions where the "verb" is a
# pseudo-token naming the world-state condition.
#
# IMPORTANT: every non-terminal state must have at least one
# outgoing entry (I-1). Every non-initial state must be the target
# of at least one entry (I-2). :func:`verify_state_machine` enforces
# both at startup + CI.
TRANSITIONS: Mapping[
    SMState,
    Mapping[Verbs | str, TransitionTo | SelfLoop | EventTransition],
] = {
    SMState.DRAFT: {
        Verbs.ROUTE_TO_STUDY: TransitionTo(SMState.NEEDS_STUDY, art_swap=True),
        Verbs.REJECT: TransitionTo(SMState.REJECTED),
        Verbs.CONTINUE: SelfLoop(),
    },
    SMState.NEEDS_STUDY: {
        Verbs.STUDY_COMPLETE: TransitionTo(SMState.SELECTED, art_swap=True),
        Verbs.STUDY_BLOCKED: TransitionTo(SMState.BLOCKED),
        Verbs.STUDY_REJECTED: TransitionTo(SMState.REJECTED),
        Verbs.CONTINUE: SelfLoop(),
        "vault-auto-advance": EventTransition(
            SMState.SELECTED, trigger="resolves-issue-in-vault"
        ),
    },
    SMState.SELECTED: {
        Verbs.RETURN_TO_STUDY: TransitionTo(SMState.NEEDS_STUDY),
        Verbs.CONTINUE: SelfLoop(),
        "linked-pr-open": EventTransition(SMState.REVIEWING, trigger="linked-pr-open"),
        "spawn-dispatch-art-code": EventTransition(
            SMState.DESIGNING, trigger="spawn-dispatch-art-code"
        ),
        "dep-rejected": EventTransition(SMState.BLOCKED, trigger="dep-rejected"),
    },
    SMState.DESIGNING: {
        Verbs.DESIGN_READY: TransitionTo(SMState.DESIGN_REVIEW),
        Verbs.CONTINUE: SelfLoop(),
    },
    SMState.DESIGN_REVIEW: {
        Verbs.DESIGN_APPROVED: TransitionTo(SMState.DESIGNED),
        Verbs.DESIGN_REVISE: TransitionTo(SMState.DESIGNING),
        Verbs.DESIGN_REJECTED: TransitionTo(SMState.REJECTED),
        Verbs.CONTINUE: SelfLoop(),
    },
    SMState.DESIGNED: {
        Verbs.CONTINUE: SelfLoop(),
        "build-spawn-dispatch": EventTransition(
            SMState.BUILDING, trigger="build-spawn-dispatch"
        ),
        "compact-signal-drop": EventTransition(
            SMState.COMPACTING, trigger="compact-signal-drop"
        ),
    },
    SMState.COMPACTING: {
        Verbs.BUILD_STARTED: TransitionTo(SMState.BUILDING),
        Verbs.CONTINUE: SelfLoop(),
    },
    SMState.BUILDING: {
        Verbs.CONTINUE: SelfLoop(),
        "linked-pr-open": EventTransition(
            SMState.REVIEWING, trigger="linked-pr-open"
        ),
    },
    SMState.REVIEWING: {
        Verbs.CONTINUE: SelfLoop(),
        "pr-merged-master-green-verified": EventTransition(
            SMState.DONE, trigger="pr-merged-master-green-verified"
        ),
        "pr-merged-master-red": EventTransition(
            SMState.BUILDING, trigger="pr-merged-master-red"
        ),
        "pr-closed-unmerged": EventTransition(
            SMState.REJECTED, trigger="pr-closed-unmerged"
        ),
    },
    SMState.DONE: {
        # Terminal. Only the close-gate verb (exit-transition for
        # art:research_note tails) acts here, and it doesn't change
        # the sm:* label — it just records the GitHub-side close.
        # Listed for parser visibility; dispatcher treats DONE as
        # terminal for the I-1 / I-2 checks.
        Verbs.EXIT_TRANSITION: TransitionTo(SMState.DONE),  # idempotent self
    },
    SMState.REJECTED: {
        # Terminal. No outgoing verb. Entry intentionally empty —
        # presence of the key satisfies the "every state in table"
        # startup check; emptiness is OK because the terminal flag
        # in STATE_META exempts it from I-1 (completeness).
    },
    SMState.BLOCKED: {
        Verbs.UNBLOCK: EventTransition(SMState.BLOCKED, trigger="restore-prior-state"),
        # The unblock verb is special: the dispatcher reads the prior
        # state from cortex-memory/gh-state/<repo>-<N>.md and routes
        # there, not back to BLOCKED. The EventTransition entry above
        # is a placeholder so I-1 (completeness) holds without
        # encoding every-possible-prior-state in the table.
    },
}
