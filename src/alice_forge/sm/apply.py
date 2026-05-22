"""Apply a :class:`HandlerResult` against real services — v3 ownership.

Phase 4 of the SM v3 rollout (issue #301) flips v3 from dry-run /
dual-run shadow to authoritative for transition decisions. This
module is the single place a :class:`HandlerResult` is turned into
real side-effects (label edits, audit comments, ledger writes,
parse-error replies).

Centralising application here is the structural guarantee that the
six invariants in the design doc hold uniformly:

  * Every emitted side-effect lands in the unified ledger
    (:class:`alice_forge.sm.ledger.EmittedRecord`).
  * Every state transition emits the canonical audit comment.
  * Every parse error produces a loud reply (I-6).
  * No handler can forget to clear the ledger on its error path —
    handlers don't call ``post_comment`` / ``edit_labels`` directly.

The dispatcher's main loop calls :func:`apply_result` once per v3
handler return. v1 legacy handlers are only invoked for issues
where v3 returned ``None`` or a non-transition result, so this
function does NOT need to undo v1's behaviour.

Side-effect-name conventions (used as the ledger ``side_effect``
key — must be stable across releases so dedup history survives):

  * ``"transition:<from>-><to>"`` — audit record for one state move.
  * ``"continue:<state>"`` — continue self-loop on the given state.
  * ``"parse-error-reply"`` — loud reply for a malformed ``[SM]``
    comment (1-hour TTL per design I-6).
  * SideEffect.name — handler-supplied identifier (e.g.
    ``"triage-surface"``, ``"spawn-started"``).
"""

from __future__ import annotations

from typing import Any

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
from alice_forge.sm.states import SMState


# Default TTL for a parse-error reply ledger record (design I-6):
# dedup repeat malformed-comment attempts within the hour so a stuck
# author doesn't get spammed with replies.
PARSE_ERROR_REPLY_TTL_SECONDS = 60 * 60


def apply_result(
    *,
    issue: dict[str, Any],
    current_state: SMState,
    result: HandlerResult,
    services: HandlerServices,
) -> bool:
    """Apply a v3 handler's :class:`HandlerResult` against real services.

    Returns ``True`` if the issue's ``sm:*`` label was changed by
    this call (i.e., a :class:`Transition` or a
    :class:`BlockedByTTL` fired). The dispatcher uses this signal to
    decide whether to skip the legacy v1 handler for this cadence —
    if v3 transitioned, v1 must not run, or it would re-emit the
    label edit + audit comment.

    All other variants return ``False``: continues, side-effects,
    no-progress strikes, and parse-error replies do not change the
    state label, so v1 still gets a chance to do its (non-
    transitioning) work for the cadence.

    Failures inside an apply step are logged and swallowed. The
    dispatcher's primary contract is that one issue's bad side-
    effect can't derail the whole cadence; the ledger is the source
    of truth on what actually shipped.
    """
    number = issue["number"]

    if isinstance(result, Transition):
        _apply_transition(
            issue=issue,
            number=number,
            current_state=current_state,
            transition=result,
            services=services,
        )
        return True

    if isinstance(result, Continue):
        _apply_continue(
            number=number,
            current_state=current_state,
            cont=result,
            services=services,
        )
        return False

    if isinstance(result, SideEffect):
        _apply_side_effect(
            number=number,
            side_effect=result,
            services=services,
        )
        return False

    if isinstance(result, NoProgress):
        _apply_no_progress(
            number=number,
            current_state=current_state,
            no_progress=result,
            services=services,
        )
        return False

    if isinstance(result, BlockedByTTL):
        _apply_blocked_by_ttl(
            issue=issue,
            number=number,
            current_state=current_state,
            blocked=result,
            services=services,
        )
        return True

    if isinstance(result, EmitParseError):
        _apply_parse_error(
            number=number,
            parse_error=result,
            services=services,
        )
        return False

    services.log(
        f"[sm-v3] apply_result #{number}: unknown HandlerResult "
        f"variant {type(result).__name__} — dropping"
    )
    return False


def _apply_transition(
    *,
    issue: dict[str, Any],
    number: int,
    current_state: SMState,
    transition: Transition,
    services: HandlerServices,
) -> None:
    """Apply a state transition: swap labels, post the audit comment,
    record the move in the ledger.

    The audit comment format mirrors v1's
    ``render_transition_comment`` so existing scrapers and the
    dual-run diff harness keep parsing the same shape.
    """
    add_labels = [transition.target.label]
    remove_labels = [current_state.label]

    # Optional art-label swap (route-to-study, study-complete).
    if transition.art_swap is not None:
        current_art = _current_art_label(issue)
        if transition.art_swap != current_art:
            add_labels.append(transition.art_swap)
            if current_art is not None:
                remove_labels.append(current_art)

    audit_body = _render_transition_audit(
        from_state=current_state,
        to_state=transition.target,
        reason=transition.reason,
        metadata=transition.metadata,
    )

    try:
        services.edit_labels(
            services.repo,
            number,
            add=add_labels,
            remove=remove_labels,
        )
        services.post_comment(services.repo, number, audit_body)
    except Exception as exc:  # noqa: BLE001 — IO failure is logged + swallowed
        services.log(
            f"[sm-v3] apply transition #{number} "
            f"{current_state.label} -> {transition.target.label} failed: {exc}"
        )
        return

    services.ledger.mark_emitted(
        issue_number=number,
        side_effect=f"transition:{current_state.value}->{transition.target.value}",
        emitted_at=services.now(),
        ttl_seconds=None,
        metadata={
            "reason": transition.reason,
            "art_swap": transition.art_swap,
            **transition.metadata,
        },
    )

    services.log(
        f"[sm-v3] transitioned #{number}: "
        f"{current_state.label} -> {transition.target.label} ({transition.reason})"
    )


def _apply_continue(
    *,
    number: int,
    current_state: SMState,
    cont: Continue,
    services: HandlerServices,
) -> None:
    """Record a continue self-loop in the ledger.

    No GitHub comment is posted here — the continue verb is *itself*
    a comment the agent already wrote. The dispatcher's job is to
    record that the continue was acknowledged so the hash-dedup
    check on the next cadence has the history it needs.

    Phase 3's continue-verb enforcement (#312) layers strike
    tracking on top of these records; Phase 4 stays out of strike
    logic and just persists the audit trail.
    """
    findings = cont.findings or ""
    services.ledger.mark_emitted(
        issue_number=number,
        side_effect=f"continue:{current_state.value}",
        emitted_at=services.now(),
        ttl_seconds=None,
        metadata={"reason": cont.reason, "findings": findings},
    )
    services.log(
        f"[sm-v3] continue #{number} at {current_state.label}: "
        f"reason={cont.reason!r}"
        + (f" findings={findings!r}" if findings else "")
    )


def _apply_side_effect(
    *,
    number: int,
    side_effect: SideEffect,
    services: HandlerServices,
) -> None:
    """Emit the side-effect's comment body and record the emission.

    Idempotency is the handler's responsibility — the handler checks
    :meth:`EmittedLedger.is_emitted_active` before returning a
    SideEffect, so by the time we apply we trust the handler's
    decision to emit.
    """
    try:
        services.post_comment(services.repo, number, side_effect.body)
    except Exception as exc:  # noqa: BLE001
        services.log(
            f"[sm-v3] apply side-effect {side_effect.name!r} "
            f"#{number} post_comment failed: {exc}"
        )
        return

    services.ledger.mark_emitted(
        issue_number=number,
        side_effect=side_effect.name,
        emitted_at=services.now(),
        ttl_seconds=side_effect.ttl_seconds,
        metadata=dict(side_effect.metadata),
    )
    services.log(
        f"[sm-v3] side-effect {side_effect.name!r} #{number} emitted"
    )


def _apply_no_progress(
    *,
    number: int,
    current_state: SMState,
    no_progress: NoProgress,
    services: HandlerServices,
) -> None:
    """Post the polite no-progress ping (design § Decisions decision 3).

    Strike counting + escalation to ``sm:blocked`` on the third
    strike is Phase 3 dispatcher-side logic (#312); Phase 4 just
    posts the reply and records it.
    """
    body = (
        f"[SM] no-progress reason=\"continue reason matches a recent prior "
        f"continue on this issue at {current_state.label}. Add new "
        f"information (findings, partial output, refined blocker) on the "
        f"next iteration.\"\n\n"
        f"Most recent matching continue was emitted at "
        f"{no_progress.duplicate_of_emitted_at}."
    )
    try:
        services.post_comment(services.repo, number, body)
    except Exception as exc:  # noqa: BLE001
        services.log(
            f"[sm-v3] apply no-progress #{number} post_comment failed: {exc}"
        )
        return

    services.ledger.mark_emitted(
        issue_number=number,
        side_effect=f"no-progress:{current_state.value}",
        emitted_at=services.now(),
        ttl_seconds=None,
        metadata={
            "duplicate_reason": no_progress.duplicate_reason,
            "duplicate_of_emitted_at": no_progress.duplicate_of_emitted_at,
        },
    )
    services.log(
        f"[sm-v3] no-progress strike #{number} at {current_state.label}"
    )


def _apply_blocked_by_ttl(
    *,
    issue: dict[str, Any],
    number: int,
    current_state: SMState,
    blocked: BlockedByTTL,
    services: HandlerServices,
) -> None:
    """Auto-transition the issue to ``sm:blocked`` (design § Decisions 3).

    The audit comment records the TTL that elapsed so the operator
    can see why the block fired without digging through ledger
    records.
    """
    reason = (
        f"TTL elapsed: no progress signal within "
        f"{blocked.state_ttl_seconds}s on {current_state.label}"
    )
    transition = Transition(
        target=SMState.BLOCKED,
        reason=reason,
        metadata={
            "blocked_by_ttl": True,
            "state_ttl_seconds": blocked.state_ttl_seconds,
            "prior_state": current_state.value,
        },
    )
    _apply_transition(
        issue=issue,
        number=number,
        current_state=current_state,
        transition=transition,
        services=services,
    )


def _apply_parse_error(
    *,
    number: int,
    parse_error: EmitParseError,
    services: HandlerServices,
) -> None:
    """Post the loud parse-error reply (invariant I-6).

    Dedup'd by the ``parse-error-reply`` ledger record with a 1-hour
    TTL so a malformed-comment loop can't spam the issue thread.
    """
    if services.ledger.is_emitted_active(
        number, "parse-error-reply", services.now()
    ):
        services.log(
            f"[sm-v3] parse-error #{number}: reply already in flight; "
            f"suppressing duplicate"
        )
        return

    try:
        services.post_comment(services.repo, number, parse_error.reply_body)
    except Exception as exc:  # noqa: BLE001
        services.log(
            f"[sm-v3] apply parse-error reply #{number} failed: {exc}"
        )
        return

    services.ledger.mark_emitted(
        issue_number=number,
        side_effect="parse-error-reply",
        emitted_at=services.now(),
        ttl_seconds=PARSE_ERROR_REPLY_TTL_SECONDS,
        metadata={
            "verb": parse_error.verb,
            "reason": parse_error.reason,
        },
    )
    services.log(
        f"[sm-v3] parse-error reply #{number} verb={parse_error.verb!r}"
    )


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------


def _render_transition_audit(
    *,
    from_state: SMState,
    to_state: SMState,
    reason: str,
    metadata: dict[str, Any],
) -> str:
    """Render the canonical ``[SM] transition`` audit comment.

    Format chosen to match v1's
    ``alice_forge.dispatcher.rendering.render_transition_comment``
    output shape so external scrapers (dual-run diff job, log
    parsers, gh-state mirror) keep working without changes.
    """
    body = (
        f"[SM] transition from={from_state.label} to={to_state.label} "
        f"reason={reason!r}"
    )
    if metadata:
        extras = " ".join(
            f"{k}={v!r}" for k, v in metadata.items() if v is not None
        )
        if extras:
            body += f"\n\n{extras}"
    return body


def _current_art_label(issue: dict[str, Any]) -> str | None:
    """Return the issue's current ``art:*`` label, or ``None``.

    Mirrors v1's :func:`_current_art_label` (in
    ``dispatcher.helpers``) so the art-swap behaviour during a
    transition stays identical pre/post Phase 4.
    """
    labels = issue.get("labels") or []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.startswith("art:"):
            return name
    return None
