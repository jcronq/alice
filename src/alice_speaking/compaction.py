"""Context-compaction helpers — pure logic, isolated from the daemon.

The daemon does two compaction-related things per turn:

1. After each ``ResultMessage``, consult ``usage.input_tokens``; if it
   crosses ``cfg.speaking["context_compaction_threshold"]``, flag the
   session for compaction on the next turn.
2. Before the next turn dispatches, if the flag is set, run a silent
   compaction turn that produces a structured 4-part summary, write it
   to ``inner/state/context-summary.md``, and roll the session.

This module owns the pure-logic bits: threshold extraction from a
ResultMessage.usage payload, the compaction prompt template, and the
summary-injection preamble builder. The stateful orchestration stays
in daemon.py where the SDK's ``query`` coroutine lives.
"""

from __future__ import annotations

import pathlib
from typing import Any, Iterable, Optional

from .turn_log import Turn, render_for_prompt


DEFAULT_THRESHOLD = 150_000


COMPACTION_PROMPT = (
    "[Internal — context compaction. Do NOT call send_message or any "
    "outbound tool. This turn produces a summary for your own use and "
    "then ends.]\n\n"
    "Before we continue, summarize our conversation in this exact "
    "structure. Keep the whole thing under 600 words.\n\n"
    "1. Active threads — open questions and pending tasks the owner "
    "mentioned that are not yet resolved.\n"
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


__all__ = [
    "COMPACTION_PROMPT",
    "DEFAULT_THRESHOLD",
    "build_bootstrap_preamble",
    "build_summary_preamble",
    "read_summary_if_any",
    "should_compact",
    "write_summary",
]
