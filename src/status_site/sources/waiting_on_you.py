"""Waiting on you — hybrid section.

Two inputs concatenated (hand-zone first, then structured asks):

1. ``inner/status/waiting-on-you.md`` — Alice's longer-form prose
   asks. Rendered verbatim. Missing / empty file is tolerated (the
   section can still have structured-queue items below).
2. ``inner/status/asks.jsonl`` — one JSON object per ask. Unresolved
   records (``resolved: false``) render as ``- [ ] <text>`` lines,
   oldest first (stable iteration by ``created``).

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

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from status_site.sources.common import read_asks, staleness_footer

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
    """Return records with ``resolved != True``, oldest first."""
    pending = [r for r in records if not r.get("resolved")]
    pending.sort(key=_ask_sort_key)
    return pending


def render_structured(records: Sequence[dict]) -> str:
    """Render unresolved ``[ ]`` lines from the asks list.

    Empty input returns ``""`` (caller decides whether to show the
    "no asks" string or just omit the section).
    """
    pending = unresolved_asks(records)
    if not pending:
        return ""
    return "\n".join(f"- [ ] {r.get('text', '').strip()}" for r in pending)


def _split_title_gist(text: str) -> tuple[str, str]:
    """Extract (title, gist) from an ask's ``text`` body.

    Title = first non-empty line, gist = remaining lines flattened
    to one space-separated line. Both are returned stripped. Empty
    input → ``("", "")``.
    """
    body = (text or "").strip()
    if not body:
        return "", ""
    lines = [ln.strip() for ln in body.splitlines()]
    non_empty = [ln for ln in lines if ln]
    if not non_empty:
        return "", ""
    title = non_empty[0]
    gist = " ".join(non_empty[1:]).strip()
    return title, gist


def _time_ago(created: str, now: datetime | None) -> str:
    """Compact "Nm" / "Nh" / "Nd" age for the todo-meta slot.

    Missing/malformed timestamp → ``""``.
    """
    if not created:
        return ""
    try:
        # asks.jsonl timestamps are ISO-8601 UTC ("...Z" or "+00:00").
        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    delta_sec = max(0, int((ref - ts).total_seconds()))
    if delta_sec < 60:
        return f"{delta_sec}s"
    minutes = delta_sec // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rem_min = minutes % 60
        return f"{hours}h" + (f" {rem_min}m" if rem_min else "")
    days = hours // 24
    return f"{days}d"


def render_cards(
    records: Sequence[dict], *, now: datetime | None = None
) -> str:
    """Emit the Agent UI collapsed todo-card HTML for each unresolved ask.

    Shape per item (phase 1 — expanded state, intent picker, reply
    composer, Resolve/Dismiss are deferred to phase 2):

        <li class="todo-card">
          <div class="todo-row">
            <button class="todo-header"><strong>{title}</strong></button>
            <span class="todo-gist">{first-line-of-body-after-title}</span>
            <span class="todo-meta">{timeAgo(createdAt)}</span>
            <span class="pill-btn todo-pill">Reply</span>
          </div>
        </li>

    Returns an empty string when there are no unresolved asks so the
    caller can fall back to the markdown-checkbox path. All text is
    HTML-escaped; the gist is truncated via CSS ``text-overflow:
    ellipsis`` so this function hands over the full remaining body.
    """
    pending = unresolved_asks(records)
    if not pending:
        return ""
    parts: list[str] = ['<ul class="todo-list" data-woy-cards="1">']
    for rec in pending:
        title, gist = _split_title_gist(str(rec.get("text", "")))
        if not title:
            continue
        age = _time_ago(str(rec.get("created") or ""), now)
        pieces = [
            '<li class="todo-card">',
            '<div class="todo-row">',
            '<button class="todo-header" type="button">',
            f"<strong>{html.escape(title)}</strong>",
            "</button>",
        ]
        if gist:
            pieces.append(f'<span class="todo-gist">{html.escape(gist)}</span>')
        if age:
            pieces.append(f'<span class="todo-meta">{html.escape(age)}</span>')
        pieces.append('<span class="pill-btn todo-pill">Reply</span>')
        pieces.append("</div></li>")
        parts.append("".join(pieces))
    parts.append("</ul>")
    return "\n".join(parts)


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
