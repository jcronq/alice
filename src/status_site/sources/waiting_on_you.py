"""Waiting on you — hybrid section.

Two inputs concatenated (hand-zone first, then structured asks):

1. ``inner/status/waiting-on-you.md`` — Alice's longer-form prose
   asks. Rendered verbatim. Missing / empty file is tolerated (the
   section can still have structured-queue items below).
2. ``inner/status/asks.jsonl`` — one JSON object per ask. Unresolved
   records (``resolved: false``) render as ``- [ ] <text>`` lines,
   urgency-sorted by ``classify`` (``urgency.py``, #641 port) and
   then oldest-first within a rank.

Resolved asks are excluded here — they surface under the Done-today
section via ``resolved_at`` filtering. The hand-zone content does
NOT carry a staleness footer (it's not polled); the structured
sub-list does, so the section as a whole gets red-tinted if its
background refresh falls behind.

This renderer intentionally does not participate in the
``FORBIDDEN`` persona check — it emits ``- [ ] <text>`` as-is, no
agent-name interpolation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

from status_site.sources.common import read_asks, staleness_footer
from status_site.urgency import (
    blocked_mark_html,
    classify,
    parse_inline_tag,
    pill_html,
    sort_key,
)

SOURCE_NAME = "asks-jsonl"


def read_hand_zone(hand_zone_path: Path) -> str:
    """Return the hand-zone file's body, stripped. Empty string when
    the file is missing or unreadable — the server must degrade
    gracefully if the mind repo hasn't been bootstrapped yet.
    """
    try:
        return hand_zone_path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _ask_sort_key(ask: dict) -> str:
    """Oldest first by ``created`` timestamp. Missing/malformed times
    sort at the bottom (empty string) so new asks without a clock
    still render deterministically."""
    v = ask.get("created")
    return v if isinstance(v, str) else ""


def unresolved_asks(records: Sequence[dict]) -> list[dict]:
    """Return records with ``resolved != True``, urgency- then
    oldest-first."""
    pending = [r for r in records if not r.get("resolved")]
    # Primary sort: oldest-first by ``created``. Secondary pass sorts
    # by urgency rank so NOW/UNSET rise to the top without breaking
    # within-rank ordering (list.sort is stable).
    pending.sort(key=_ask_sort_key)
    pending.sort(key=lambda r: sort_key(r))
    return pending


def _format_ask_line(record: dict) -> str:
    """Render one ask as a ``- [ ] <pill> <blocked?> <text>`` line.

    The urgency pill is an inline HTML span (``fx-urg``); GFM passes
    it through untouched. An explicit ``blocks`` / ``blocked`` flag
    adds the dashboard's ``fx-mark blocked`` marker in front of the
    text.
    """
    raw_text = str(record.get("text") or "").strip()
    inline_rank, remaining = parse_inline_tag(raw_text)
    body_text = remaining if inline_rank else raw_text
    rank = classify(record)
    pill = pill_html(rank)
    blocked = bool(record.get("blocks") or record.get("blocked"))
    mark = blocked_mark_html("blocked") + " " if blocked else ""
    return f"- [ ] {pill} {mark}{body_text}".rstrip()


def render_structured(records: Sequence[dict]) -> str:
    """Render unresolved ``[ ]`` lines from the asks list.

    Each line carries an urgency pill (``fx-urg RANK``) and, when the
    ask is flagged blocked, a leading ``fx-mark.blocked`` marker.
    Empty input returns ``""`` (caller decides whether to show the
    "no asks" string or just omit the section).
    """
    pending = unresolved_asks(records)
    if not pending:
        return ""
    return "\n".join(_format_ask_line(r) for r in pending)


def render(
    hand_zone_path: Path,
    asks_records: Sequence[dict] | None = None,
    *,
    asks_path: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Compose hand-zone + structured-list + staleness footer.

    Either ``asks_records`` (for tests) or ``asks_path`` (for
    production) must be provided; if both are None, renders only the
    hand zone + an empty structured block."""
    if asks_records is None:
        asks_records = read_asks(asks_path) if asks_path else []
    hand = read_hand_zone(hand_zone_path)
    structured = render_structured(asks_records)

    parts: list[str] = []
    if hand:
        parts.append(hand)
    if structured:
        parts.append(structured)
    if not parts:
        parts.append("*(nothing waiting on you)*")

    body = "\n\n".join(parts)
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)
