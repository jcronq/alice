"""Context-compaction helpers + trigger.

The daemon does two compaction-related things per turn:

1. After each ``ResultMessage``, consult ``usage.input_tokens``; if it
   crosses ``cfg.speaking["context_compaction_threshold"]``, arm the
   :class:`CompactionTrigger` for compaction on the next turn.
2. Before the next turn dispatches, if the trigger says to fire, run a
   silent compaction turn that produces a structured 4-part summary,
   write it to ``inner/state/context-summary.md``, and roll the
   session.

Plan 01 Phase 6b extracted :class:`CompactionTrigger` from
daemon-side state. The pure-logic helpers (threshold check, prompt
text, preamble builders, summary-file IO) stay as module functions
since they're stateless.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from typing import Any, Iterable, Optional

from .turn_log import Turn, render_for_prompt


log = logging.getLogger(__name__)


DEFAULT_THRESHOLD = 150_000


COMPACTION_PROMPT = (
    "[Internal — context compaction. Do NOT call send_message or any "
    "outbound tool. This turn produces a summary for your own use and "
    "then ends.]\n\n"
    "Before we continue, summarize our conversation in this exact "
    "structure. Keep the whole thing under 600 words.\n\n"
    "1. Active threads — open questions and pending tasks the owner or "
    "trusted contacts mentioned that are not yet resolved.\n"
    "2. The owner's current state — mood, schedule, what they're working "
    "on, what they're avoiding, energy level.\n"
    "3. Recent surface verdicts — decisions that shaped your behavior "
    "this session (e.g. 'voiced / filed / let pass').\n"
    "4. Uncaptured facts — anything established in this session that "
    "isn't yet in cortex-memory and should be.\n\n"
    "This summary becomes your bootstrap context after the session "
    "rolls. Write it as plain prose under the four headings. Reply "
    "with only the summary text — no preamble, no apology, no "
    "closing remark."
)


def should_compact(
    usage: Optional[dict[str, Any]], threshold: int
) -> bool:
    """Return True when the effective context size from the last turn
    exceeds the threshold.

    "Effective" = input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens. For Signal turns the SDK accumulates
    cache_read_input_tokens across all API calls in a single query()
    invocation; input_tokens alone is always tiny (7-23) and would
    never cross the threshold.

    Tolerates missing / non-integer fields — a missing usage dict is
    treated as "no token pressure" (better to skip than compact on
    phantom data).
    """
    if not usage or not isinstance(usage, dict):
        return False
    try:
        effective = (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
    except (TypeError, ValueError):
        return False
    return effective > threshold


def build_summary_preamble(
    summary_text: str, recent_turns: Iterable[Turn]
) -> str:
    """Compose the preamble injected at the start of a rolled session.

    Combines the compaction summary with the verbatim tail of the turn
    log so the fresh session can bridge the gap between "summary
    cutoff" and "what just happened". Per the v3 design.
    """
    transcript = render_for_prompt(recent_turns)
    parts = [
        "[Context summary — session rolled after compaction / daemon restart]",
        "",
        summary_text.strip(),
    ]
    if transcript:
        parts.extend(
            [
                "",
                "---",
                "Recent turns:",
                transcript,
            ]
        )
    return "\n".join(parts)


def build_bootstrap_preamble(recent_turns: Iterable[Turn]) -> str:
    """Compose the Layer 2 bootstrap preamble — turn_log only, no
    compaction summary. Used when the daemon starts cold (no
    ``session.json`` or the SDK session JSONL is gone)."""
    transcript = render_for_prompt(recent_turns)
    if not transcript:
        return ""
    return (
        "[Daemon restart — context restoration from turn log]\n\n"
        f"Recent conversation:\n{transcript}\n\n"
        "Resume naturally."
    )


def read_summary_if_any(path: pathlib.Path) -> Optional[str]:
    """Read the on-disk compaction summary if present."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return text or None


def write_summary(path: pathlib.Path, text: str) -> None:
    """Write the compaction summary atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text.rstrip() + "\n")
    tmp.replace(path)


class CompactionTrigger:
    """Encapsulates the pending-compaction flag and the run logic.

    Replaces the inline ``self._compaction_pending`` + ``_run_compaction``
    pair on :class:`SpeakingDaemon`. Both consumers (the dispatcher
    main loop and SignalTransport's per-transport batch loop) call
    :meth:`should_run` followed by :meth:`run` instead of the bare
    flag check, so the deferral policy lives in one place.

    The plan-01 design specifies a deep-thread deferral for
    SignalEvents (defer compaction up to 5 turns when a DEEP-depth
    design conversation is in flight). Phase 2a relocated Signal
    onto its own consumer loop, so the deferral logic is the
    Signal consumer's concern — :meth:`should_run` here only owns
    the pending flag. The deferral hook stays a TODO until the
    SessionDepthSignal landing pad exists.
    """

    MAX_DEFERRAL_TURNS = 5

    def __init__(self) -> None:
        self._pending: bool = False
        self._deferred_turns: int = 0

    # ------------------------------------------------------------------
    # State accessors

    def arm(self) -> None:
        """Signal that the next dispatcher loop should fire compaction.

        Called by :class:`alice_speaking.handlers.CompactionArmer` on
        each ``ResultMessage`` whose ``usage`` crosses the configured
        threshold.
        """
        self._pending = True

    def pending(self) -> bool:
        return self._pending

    def should_run(self, event: Any) -> bool:
        """Should the dispatcher run compaction before handling this event?

        Today: returns the pending flag. Reserved for the deep-thread
        deferral policy described in the plan; that needs the
        SessionDepthSignal landing pad before it can ship.
        """
        return self._pending

    # ------------------------------------------------------------------
    # Execution

    async def run(self, ctx: Any) -> None:
        """Run a silent compaction turn, write the summary, roll the session.

        Reads from / writes to ``ctx`` for the operations that touch
        daemon-shared state (``ctx._run_turn``, ``ctx.session_id``,
        ``ctx._summary_path``, ``ctx._session_path``,
        ``ctx._prime_bootstrap_preamble``, ``ctx.events``,
        ``ctx._compaction_pending`` clears) — this method *is* the
        orchestration that used to live as ``SpeakingDaemon._run_compaction``;
        Phase 6 will narrow ``ctx`` to a real interface so the seam is
        explicit.
        """
        # Lazy imports — these create cycles if pulled at module top.
        from . import session_state

        turn_id = f"compact-{uuid.uuid4().hex[:8]}"
        ctx.events.emit("context_compaction_start", turn_id=turn_id)
        try:
            summary = await ctx._run_turn(
                COMPACTION_PROMPT,
                turn_id=turn_id,
                outbound_recipient=None,
                silent=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("compaction turn failed; leaving session intact")
            ctx.events.emit(
                "context_compaction_error",
                turn_id=turn_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._pending = False
            return

        if not summary:
            log.warning(
                "compaction turn returned empty summary; rolling anyway"
            )
            summary = "(compaction produced no summary — session rolled on empty)"

        try:
            write_summary(ctx._summary_path, summary)
        except OSError:
            log.exception("failed to write %s", ctx._summary_path)

        # Roll.
        old_sid = ctx.session_id
        ctx.session_id = None
        session_state.clear(ctx._session_path)
        self._pending = False
        ctx.events.emit(
            "context_compaction",
            turn_id=turn_id,
            summary_len=len(summary),
            previous_session_id=old_sid,
        )
        ctx.events.emit("session_roll", previous_session_id=old_sid)

        # Prime the next real turn with the summary injection.
        ctx._prime_bootstrap_preamble()


__all__ = [
    "COMPACTION_PROMPT",
    "CompactionTrigger",
    "DEFAULT_THRESHOLD",
    "build_bootstrap_preamble",
    "build_summary_preamble",
    "read_summary_if_any",
    "should_compact",
    "write_summary",
]
