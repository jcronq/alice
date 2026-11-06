"""Per-message emoji reactions for the Chat tab (issue #668).

Reactions live in an append-only sidecar, ``.chat-reactions.jsonl``, keyed
by the target message's ``at``. ``.chat.jsonl`` is never rewritten. One
line per event::

    {"at": "<msg-at>", "emoji": "👍", "from": "jason",
     "at_reacted": "<iso>", "action": "add"}

``action`` defaults to ``"add"`` when absent; ``"remove"`` is a tombstone
for an earlier add by the same ``from``. :func:`merge_reactions` folds the
events into each message as ``reactions: {emoji: [{from, at}]}``.
"""

from __future__ import annotations

import json
from pathlib import Path

MAX_EMOJI_CHARS = 16


def valid_emoji(value: object) -> bool:
    """A short, non-blank string with no whitespace or control characters."""
    if not isinstance(value, str) or not value or len(value) > MAX_EMOJI_CHARS:
        return False
    return not any(c.isspace() or not c.isprintable() for c in value)


def read_events(path: Path) -> list[dict]:
    """Well-formed events in file order; malformed lines are skipped."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if not isinstance(ev.get("at"), str) or not ev["at"]:
            continue
        if not valid_emoji(ev.get("emoji")):
            continue
        if not isinstance(ev.get("from"), str) or not ev["from"]:
            continue
        if ev.get("action", "add") not in ("add", "remove"):
            continue
        out.append(ev)
    return out


def fold(events: list[dict]) -> dict[str, dict[str, dict[str, str]]]:
    """``{msg_at: {emoji: {from: at_reacted}}}`` after replaying ``events``."""
    state: dict[str, dict[str, dict[str, str]]] = {}
    for ev in events:
        by_emoji = state.setdefault(ev["at"], {})
        users = by_emoji.setdefault(ev["emoji"], {})
        if ev.get("action", "add") == "remove":
            users.pop(ev["from"], None)
        else:
            users.setdefault(ev["from"], str(ev.get("at_reacted") or ""))
    return state


def reactions_for(
    state: dict[str, dict[str, dict[str, str]]], msg_at: str
) -> dict[str, list[dict]]:
    """Message-shaped reactions (``{emoji: [{from, at}]}``), empties dropped."""
    out: dict[str, list[dict]] = {}
    for emoji, users in (state.get(msg_at) or {}).items():
        if users:
            out[emoji] = [{"from": u, "at": at} for u, at in users.items()]
    return out


def merge_reactions(messages: list[dict], path: Path) -> list[dict]:
    """Attach ``reactions`` to each message that has any (mutates + returns).

    A missing sidecar, malformed lines, and events whose ``at`` matches no
    message are all tolerated.
    """
    state = fold(read_events(path))
    if not state:
        return messages
    for m in messages:
        at = m.get("at")
        if not isinstance(at, str):
            continue
        r = reactions_for(state, at)
        if r:
            m["reactions"] = r
    return messages
