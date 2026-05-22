"""Continue-verb enforcement for SM v3 — Phase 3.

The design's invariants I-3 (useful-comment-per-iteration) and I-4
("useful" definition with hash dedup + three-strike escalation)
require that the dispatcher *enforce* continue verbs, not just
accept them. This module is the enforcement layer.

Two pieces, both gated by the ``SM_REQUIRE_CONTINUE`` env var
(default OFF for the Phase 3 PR):

  1. **Per-cycle strike accounting.** When a handler returns
     :class:`NoProgress` (a duplicate-continue hash hit) or a bare
     :class:`Continue` that the dispatcher detects as a duplicate
     of a recent same-issue continue, the dispatcher posts the
     three-strike sequence:

       * Strike 1: polite no-progress reply that names the duplicate
         and asks for new information.
       * Strike 2: stronger warning that the next strike escalates.
       * Strike 3: transition to ``sm:blocked`` with an audit comment.

     Strikes are stored in the unified emit ledger under the
     ``"no-progress-strikes"`` side-effect name with the count in
     ``metadata["count"]``. A non-duplicate continue or any
     transition clears the record.

  2. **One-time grace transition.** When the flag flips from OFF to
     ON, in-flight issues whose most-recent ``[SM] continue`` comment
     is older than the state's TTL get a one-time transition to
     ``sm:blocked``. Idempotent — recorded in the ledger as
     ``"grace-transition"`` so it fires once per issue.

The flag check goes through :func:`is_enforcement_enabled` so tests
can monkey-patch the env var.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable

from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    BlockedByTTL,
    Continue,
    HandlerResult,
    NoProgress,
    SideEffect,
    Transition,
)
from alice_forge.sm.states import STATE_META, SMState


# ----------------------------------------------------------------
# Flag
# ----------------------------------------------------------------

ENFORCEMENT_ENV_VAR = "SM_REQUIRE_CONTINUE"

# Side-effect names on the unified emit ledger.
STRIKES_SIDE_EFFECT = "no-progress-strikes"
GRACE_TRANSITION_SIDE_EFFECT = "grace-transition"

# Strike threshold — at the third strike we transition to blocked.
STRIKE_LIMIT = 3


def is_enforcement_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True iff the ``SM_REQUIRE_CONTINUE`` env var is set to
    a truthy value (``"1"``, ``"true"``, ``"yes"`` — case-insensitive).

    Pass an explicit ``env`` mapping for tests; otherwise reads
    :data:`os.environ`.
    """
    source = env if env is not None else os.environ
    raw = source.get(ENFORCEMENT_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ----------------------------------------------------------------
# Strike accounting
# ----------------------------------------------------------------


# Strike-1 and Strike-2 reply bodies. The contract is set by the
# design doc (§ Decisions, item 3) and is part of the user-visible
# protocol — change only with a doc update.
STRIKE_1_TEMPLATE = (
    '[SM] no-progress reason="continue-{verb} reason matches the most '
    "recent continue. Add new information (findings, partial output, "
    'refined blocker) on the next iteration."'
)
STRIKE_2_TEMPLATE = (
    '[SM] no-progress reason="continue-{verb} reason still matches the '
    'recent continue — second strike. Next strike escalates to sm:blocked. '
    'Add new information (findings, partial output, refined blocker) on '
    'the next iteration."'
)
STRIKE_3_AUDIT_TEMPLATE = (
    '[SM] transition from={state} to=blocked reason="three no-progress '
    'strikes — agent not advancing the issue. Use [SM] unblock when '
    'status changes."'
)


@dataclass(frozen=True)
class StrikeAction:
    """The outcome of one strike-accounting pass.

    ``kind`` is one of:

      * ``"pass-through"`` — no enforcement applied, return the handler
        result unchanged.
      * ``"strike-1"`` / ``"strike-2"`` — emit the no-progress reply
        and bump the ledger counter.
      * ``"strike-3-block"`` — emit the audit comment and transition
        to ``sm:blocked``.

    ``side_effect`` carries the comment body and the ledger record
    metadata. ``transition`` is set only for the strike-3 case.
    """

    kind: str
    side_effect: SideEffect | None = None
    transition: Transition | None = None


def _normalize_reason(reason: str) -> str:
    """Whitespace-normalize a reason for hash dedup.

    Collapses runs of whitespace, strips leading/trailing space, and
    lowercases. Matches the I-4 definition: trivially-reworded
    continues still hash to the same value if the prose is identical
    modulo whitespace.
    """
    return re.sub(r"\s+", " ", reason).strip().lower()


def _hash_reason(reason: str) -> str:
    """Stable hex digest of the normalized reason. Used for dedup."""
    normalized = _normalize_reason(reason)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_duplicate_continue(
    *,
    issue_number: int,
    reason: str,
    ledger: EmittedLedger,
    lookback: int = 3,
) -> tuple[bool, _dt.datetime | None]:
    """Return ``(is_dup, prior_emitted_at)`` for a candidate continue.

    A continue is a duplicate iff its normalized-reason hash appears
    in the previous ``lookback`` continue records for the same issue.
    The reason hash is stored in ``record.metadata["reason_hash"]``
    by :func:`record_continue`; this function reads it.
    """
    target_hash = _hash_reason(reason)
    # Walk records newest-first to find the latest matching continue.
    matches = sorted(
        [
            r
            for r in ledger.records
            if r.issue_number == issue_number
            and r.side_effect == "continue"
            and r.metadata.get("reason_hash")
        ],
        key=lambda r: r.emitted_at,
        reverse=True,
    )
    for rec in matches[:lookback]:
        if rec.metadata.get("reason_hash") == target_hash:
            return True, rec.emitted_at
    return False, None


def record_continue(
    *,
    issue_number: int,
    reason: str,
    ledger: EmittedLedger,
    now: _dt.datetime,
    findings: str | None = None,
) -> None:
    """Record a non-duplicate continue on the ledger.

    Stores the reason hash in metadata so future duplicate checks
    are O(1) per record. TTL is None — continues are not tracked for
    expiry on their own; the per-state continue-TTL is enforced
    separately by :func:`compute_grace_block`.
    """
    ledger.mark_emitted(
        issue_number=issue_number,
        side_effect="continue",
        emitted_at=now,
        ttl_seconds=None,
        metadata={
            "reason": reason,
            "reason_hash": _hash_reason(reason),
            "findings": findings,
        },
    )


def _current_strike_count(
    issue_number: int, ledger: EmittedLedger, now: _dt.datetime
) -> int:
    """Read the active strike count for ``issue``.

    Returns 0 if there's no active strike record. Scans the ledger
    directly (rather than via :meth:`EmittedLedger.find`) because
    :func:`_bump_strike` performs a tight-loop replace where the
    prior record and its replacement share the same ``emitted_at``
    timestamp; ``find``'s ``>`` (strict) tiebreak doesn't pick the
    later one. Walking the records list in append order and taking
    the last uncleared match gives the right semantic here.
    """
    latest = None
    for rec in ledger.records:
        if rec.issue_number != issue_number:
            continue
        if rec.side_effect != STRIKES_SIDE_EFFECT:
            continue
        if rec.cleared_at is not None:
            continue
        latest = rec
    if latest is None:
        return 0
    count = latest.metadata.get("count", 0)
    if isinstance(count, int):
        return count
    if isinstance(count, str):
        try:
            return int(count)
        except ValueError:
            return 0
    return 0


def _bump_strike(
    issue_number: int,
    ledger: EmittedLedger,
    now: _dt.datetime,
    reason: str,
) -> int:
    """Increment the strike counter for ``issue`` and return the new value.

    If an active strike record exists, replace its ``count`` (the
    ledger's :meth:`mark_emitted` clears the prior record with
    ``cleared_by="replaced"`` so the audit trail is preserved).
    """
    current = _current_strike_count(issue_number, ledger, now)
    new_count = current + 1
    ledger.mark_emitted(
        issue_number=issue_number,
        side_effect=STRIKES_SIDE_EFFECT,
        emitted_at=now,
        ttl_seconds=None,
        metadata={"count": new_count, "last_reason": reason},
    )
    return new_count


def clear_strikes(
    issue_number: int, ledger: EmittedLedger, now: _dt.datetime
) -> bool:
    """Clear the active strike record(s) for ``issue``.

    Called when a non-duplicate continue OR a real transition lands
    — the strike sequence resets per the design (§ Decisions, item 3).
    Returns True iff at least one record was cleared. Walks the
    ledger directly rather than going through
    :meth:`EmittedLedger.clear_emitted` because the strike-replace
    flow leaves two records with the same ``emitted_at``; the
    ledger's strict-``>`` tiebreak in :meth:`find` can pick the
    already-cleared one and skip the active record.
    """
    cleared_any = False
    for rec in ledger.records:
        if rec.issue_number != issue_number:
            continue
        if rec.side_effect != STRIKES_SIDE_EFFECT:
            continue
        if rec.cleared_at is not None:
            continue
        rec.cleared_at = now
        rec.cleared_by = "strikes-reset"
        cleared_any = True
    return cleared_any


def apply_enforcement(
    *,
    issue_number: int,
    state: SMState,
    handler_result: HandlerResult | None,
    ledger: EmittedLedger,
    now: _dt.datetime,
    env: dict[str, str] | None = None,
) -> StrikeAction:
    """Run continue-verb enforcement against one handler result.

    Called by the dispatcher after the handler runs but before the
    handler's :class:`HandlerResult` is applied. Returns a
    :class:`StrikeAction` describing whether to (a) pass the result
    through unchanged, (b) emit a no-progress strike reply (and
    record the bumped counter), or (c) override the result with a
    transition to ``sm:blocked``.

    Pass-through is the default for every case except a confirmed
    duplicate continue or a :class:`NoProgress` from the handler.

    The flag check is the first gate: when
    :func:`is_enforcement_enabled` returns False, every call returns
    ``StrikeAction("pass-through")``.
    """
    if not is_enforcement_enabled(env):
        return StrikeAction(kind="pass-through")

    # Real transitions / explicit blocks / parse errors reset the
    # strike count. They satisfy I-3 directly.
    if isinstance(handler_result, Transition):
        clear_strikes(issue_number, ledger, now)
        return StrikeAction(kind="pass-through")
    if isinstance(handler_result, BlockedByTTL):
        # The dispatcher's TTL path produces its own transition;
        # enforcement just clears the strike counter.
        clear_strikes(issue_number, ledger, now)
        return StrikeAction(kind="pass-through")
    if isinstance(handler_result, SideEffect):
        # Substantive non-continue comment — satisfies I-3, reset.
        clear_strikes(issue_number, ledger, now)
        return StrikeAction(kind="pass-through")

    # Duplicate continue: hash check against recent ledger entries.
    duplicate_reason: str | None = None
    if isinstance(handler_result, Continue):
        is_dup, _prior_at = is_duplicate_continue(
            issue_number=issue_number,
            reason=handler_result.reason,
            ledger=ledger,
        )
        if not is_dup:
            # Fresh information — record it and reset the strike count.
            record_continue(
                issue_number=issue_number,
                reason=handler_result.reason,
                ledger=ledger,
                now=now,
                findings=handler_result.findings,
            )
            clear_strikes(issue_number, ledger, now)
            return StrikeAction(kind="pass-through")
        duplicate_reason = handler_result.reason

    # Explicit NoProgress from the handler — already detected as a
    # duplicate; reason comes from the result variant.
    if isinstance(handler_result, NoProgress):
        duplicate_reason = handler_result.duplicate_reason

    if duplicate_reason is None:
        # The handler returned None or some other non-progress signal
        # without a duplicate-continue match. Don't strike — the
        # dispatcher's TTL path is the right escalation for "agent
        # produced nothing this cycle." Strikes are for the specific
        # failure mode "agent produced the SAME comment twice."
        return StrikeAction(kind="pass-through")

    new_count = _bump_strike(issue_number, ledger, now, duplicate_reason)

    verb_in_reason = _extract_verb_for_reply(state)

    if new_count < STRIKE_LIMIT:
        template = STRIKE_1_TEMPLATE if new_count == 1 else STRIKE_2_TEMPLATE
        body = template.format(verb=verb_in_reason)
        return StrikeAction(
            kind=f"strike-{new_count}",
            side_effect=SideEffect(
                name=STRIKES_SIDE_EFFECT,
                body=body,
                ttl_seconds=None,
                metadata={
                    "strike_count": new_count,
                    "duplicate_reason": duplicate_reason,
                },
            ),
        )

    # Strike 3 — escalate to blocked.
    audit_body = STRIKE_3_AUDIT_TEMPLATE.format(state=state.value)
    return StrikeAction(
        kind="strike-3-block",
        transition=Transition(
            target=SMState.BLOCKED,
            reason="three no-progress strikes — agent not advancing",
            metadata={
                "strike_count": new_count,
                "audit_body": audit_body,
                "prior_state": state.value,
            },
        ),
    )


def _extract_verb_for_reply(state: SMState) -> str:
    """Render the state's role-token for the no-progress reply.

    The reply body says ``continue-<verb>`` to mirror v1's per-state
    verb shape (``continue-needs_study`` etc.). In v3 the verb is
    just ``continue`` but the *label* in the reply still names the
    state for operator-side legibility.
    """
    return state.value.split(":", 1)[-1]


# ----------------------------------------------------------------
# One-time grace transition
# ----------------------------------------------------------------


GRACE_TRANSITION_BODY = (
    '[SM] grace-transition reason="continue-verb enforcement enabled; '
    "this issue had no continue verb within TTL. Please add a continue-* "
    'comment to unblock."'
)


def _latest_continue_timestamp(
    issue_number: int, ledger: EmittedLedger
) -> _dt.datetime | None:
    """Return the most recent ledger ``continue`` emit timestamp, or None."""
    matches = [
        r
        for r in ledger.records
        if r.issue_number == issue_number and r.side_effect == "continue"
    ]
    if not matches:
        return None
    return max(matches, key=lambda r: r.emitted_at).emitted_at


def compute_grace_block(
    *,
    issue_number: int,
    state: SMState,
    issue_last_activity: _dt.datetime,
    ledger: EmittedLedger,
    now: _dt.datetime,
    env: dict[str, str] | None = None,
) -> Transition | None:
    """One-time grace block when ``SM_REQUIRE_CONTINUE`` flips ON.

    Returns a :class:`Transition` to ``sm:blocked`` if all hold:

      1. Enforcement is enabled (``is_enforcement_enabled``).
      2. The state is non-terminal and has a TTL (terminal states
         and ``sm:blocked`` itself are exempt).
      3. ``now - issue_last_activity`` exceeds the state's TTL AND
         no ledger continue exists for this issue within TTL.
      4. No prior grace-transition has fired for this issue
         (ledger-recorded idempotency).

    On firing, records a ``"grace-transition"`` ledger entry so the
    next cycle is a no-op. ``issue_last_activity`` is the timestamp
    the dispatcher considers "most recent activity" for the issue
    (usually the latest comment ``updated_at``).
    """
    if not is_enforcement_enabled(env):
        return None

    meta = STATE_META.get(state)
    if meta is None or meta.terminal or meta.default_continue_ttl_seconds is None:
        return None

    # Idempotency — fires once per issue per enforcement cutover.
    prior = ledger.find(issue_number, GRACE_TRANSITION_SIDE_EFFECT)
    if prior is not None:
        return None

    ttl = _dt.timedelta(seconds=meta.default_continue_ttl_seconds)

    # If a recent continue is on the ledger, the issue is healthy.
    latest_continue = _latest_continue_timestamp(issue_number, ledger)
    if latest_continue is not None and now - latest_continue < ttl:
        return None

    if now - issue_last_activity < ttl:
        return None

    # Record the grace transition before returning so concurrent
    # cycles don't double-fire. The ledger persistence makes this
    # durable across dispatcher restarts.
    ledger.mark_emitted(
        issue_number=issue_number,
        side_effect=GRACE_TRANSITION_SIDE_EFFECT,
        emitted_at=now,
        ttl_seconds=None,
        metadata={
            "prior_state": state.value,
            "issue_last_activity": issue_last_activity.isoformat(),
            "ttl_seconds": meta.default_continue_ttl_seconds,
        },
    )

    return Transition(
        target=SMState.BLOCKED,
        reason=(
            "grace transition: continue-verb enforcement enabled and no "
            f"continue-* comment within {meta.default_continue_ttl_seconds}s"
        ),
        metadata={
            "audit_body": GRACE_TRANSITION_BODY,
            "prior_state": state.value,
            "trigger": "grace-transition",
        },
    )


def grace_pass_over_issues(
    *,
    issues: Iterable[tuple[int, SMState, _dt.datetime]],
    ledger: EmittedLedger,
    now: _dt.datetime,
    env: dict[str, str] | None = None,
) -> list[tuple[int, Transition]]:
    """Apply the one-time grace check across a batch of in-flight issues.

    ``issues`` is an iterable of ``(issue_number, current_state,
    last_activity_at)``. Returns the list of ``(issue_number,
    transition)`` pairs the dispatcher should apply this cadence.

    Pure orchestration over :func:`compute_grace_block`. Kept here
    so the dispatcher's main loop can call one function instead of
    looping itself.
    """
    if not is_enforcement_enabled(env):
        return []

    out: list[tuple[int, Transition]] = []
    for number, state, last_activity in issues:
        t = compute_grace_block(
            issue_number=number,
            state=state,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=now,
            env=env,
        )
        if t is not None:
            out.append((number, t))
    return out


__all__ = [
    "ENFORCEMENT_ENV_VAR",
    "STRIKE_LIMIT",
    "STRIKES_SIDE_EFFECT",
    "GRACE_TRANSITION_SIDE_EFFECT",
    "STRIKE_1_TEMPLATE",
    "STRIKE_2_TEMPLATE",
    "STRIKE_3_AUDIT_TEMPLATE",
    "GRACE_TRANSITION_BODY",
    "StrikeAction",
    "is_enforcement_enabled",
    "is_duplicate_continue",
    "record_continue",
    "clear_strikes",
    "apply_enforcement",
    "compute_grace_block",
    "grace_pass_over_issues",
]
