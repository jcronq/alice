"""Handler result variants for SM v3.

Each handler returns one :class:`HandlerResult` per dispatcher pass.
The dispatcher applies the result uniformly — handlers don't call
``edit_labels`` / ``post_comment`` directly. Centralising the
application path kills the "forgot to clear the ledger on the error
path" class of bug that plagued v1.

The six variants exhaustively cover what a v3 handler can do on one
visit:

  * :class:`Transition` — move the issue to a new state.
  * :class:`Continue` — explicit ``[SM] continue`` self-loop with a
    substantive reason (satisfies I-3).
  * :class:`SideEffect` — emit a substantive non-continue comment
    (spawn-started, study-hint-written, etc.). Also satisfies I-3
    without needing a continue.
  * :class:`NoProgress` — the agent emitted a duplicate continue
    reason; the dispatcher posts a polite no-progress reply and
    increments the strike counter. Three strikes auto-block.
  * :class:`BlockedByTTL` — the per-state TTL elapsed with no
    progress signal. The dispatcher transitions to ``sm:blocked``.
  * :class:`EmitParseError` — a ``[SM] `` comment failed to parse;
    the dispatcher posts the reply (I-6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alice_forge.sm.states import SMState


@dataclass(frozen=True)
class Transition:
    """Move the issue to ``target`` with an audit reason.

    ``target`` — the destination state.
    ``reason`` — free-form rationale rendered into the
    ``[SM] transition from=... to=... reason=...`` audit comment.
    ``art_swap`` — optional new art label to set during the
    transition (the dispatcher applies the label swap atomically
    with the state-label edit).
    ``metadata`` — additional fields to surface in the audit
    comment beyond the standard ones.
    """

    target: SMState
    reason: str
    art_swap: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Continue:
    """Explicit ``[SM] continue`` self-loop — the agent reported
    substantive progress; the dispatcher records the continue and
    moves on.

    ``reason`` — the parsed ``reason=`` field from the agent's
    continue comment.
    ``findings`` — optional wikilink to a findings note
    (``[[<slug>]]``).
    """

    reason: str
    findings: str | None = None


@dataclass(frozen=True)
class SideEffect:
    """The handler wants to emit a substantive non-continue comment.

    Examples: ``spawn-started`` when dispatching a worker,
    ``study-hint-written`` when the dispatcher writes the hint file,
    ``no-progress`` when issuing a strike. Side-effects satisfy I-3
    (the substantive-comment-per-cycle rule) on their own — the
    handler doesn't ALSO need to emit a continue.

    ``name`` — the side-effect identifier recorded in the ledger.
    ``body`` — the GitHub comment body to post.
    ``ttl_seconds`` — None means "requires explicit completion
    marker"; an int sets the auto-expiry budget.
    ``metadata`` — extra fields stored on the ledger record.
    """

    name: str
    body: str
    ttl_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NoProgress:
    """Strike — the agent's continue reason matched a recent prior
    (hash-dedup hit).

    The dispatcher posts a polite ``[SM] no-progress`` reply (or, on
    the third consecutive strike, transitions to ``sm:blocked``).
    The decision lives in the dispatcher, not the handler — the
    handler just reports that it detected a duplicate.

    ``duplicate_of_emitted_at`` — timestamp of the prior continue
    whose reason matched, for the operator's debugging.
    """

    duplicate_reason: str
    duplicate_of_emitted_at: str  # ISO timestamp of the prior matching continue


@dataclass(frozen=True)
class BlockedByTTL:
    """The state's continue-TTL elapsed without progress.

    Triggers automatic ``sm:blocked`` transition with an audit
    comment naming the failure mode. The blocking is the
    dispatcher's job; the handler signals the condition.
    """

    state_ttl_seconds: int


@dataclass(frozen=True)
class EmitParseError:
    """A ``[SM] `` comment failed to parse — emit the loud reply (I-6).

    ``verb`` — the (attempted) verb token that failed, or empty if
    the body had no verb at all.
    ``reason`` — one-line explanation to render in the reply.
    ``reply_body`` — the full body to post on the issue.
    """

    verb: str
    reason: str
    reply_body: str


# Type alias for the discriminated union of all handler results.
HandlerResult = (
    Transition | Continue | SideEffect | NoProgress | BlockedByTTL | EmitParseError
)
