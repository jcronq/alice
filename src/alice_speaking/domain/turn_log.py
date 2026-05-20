"""Persistent per-turn log for speaking Alice.

One JSONL line per processed message. Read on startup to rebuild the recent
working context and survive restarts amnesia-free. The signal-cli log is the
source of truth for *envelopes*; this file is the record of *Alice's turns*.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional


@dataclass
class Turn:
    ts: float  # unix seconds, local clock
    sender_number: str
    sender_name: str
    inbound: str
    outbound: Optional[str]  # None when we didn't reply (error, empty, skipped)
    error: Optional[str] = None
    # Cue-runner retrieval that was injected at the top of this turn's
    # prompt, captured so the per-turn log shows *what* the vault
    # surfaced. ``vault_context`` is the verbatim "VAULT CONTEXT" block
    # (rendered text). ``vault_candidates`` is the structured form —
    # one dict per top-N candidate with ``slug``/``title``/``score``/
    # ``matched_lines``/``why_relevant``. Both ``None`` for silent
    # turns, cue-runner-disabled turns, and turns that returned no
    # matches. Older log lines without these fields still parse cleanly
    # via the defaults.
    vault_context: Optional[str] = None
    vault_candidates: Optional[list[dict[str, Any]]] = None


class TurnLog:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def append(self, turn: Turn) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(turn), ensure_ascii=False) + "\n")

    def tail(self, n: int) -> list[Turn]:
        """Return the last n turns (oldest-first). Cheap on small files; fine
        for our typical working-context sizes."""
        if not self.path.is_file():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        out: list[Turn] = []
        known = {f.name for f in Turn.__dataclass_fields__.values()}
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Drop unknown keys so future field additions don't break
            # the reader; missing keys fall back to dataclass defaults.
            filtered = {k: v for k, v in obj.items() if k in known}
            try:
                out.append(Turn(**filtered))
            except TypeError:
                continue
        return out


def new_turn(
    sender_number: str,
    sender_name: str,
    inbound: str,
    outbound: Optional[str] = None,
    error: Optional[str] = None,
    vault_context: Optional[str] = None,
    vault_candidates: Optional[list[dict[str, Any]]] = None,
) -> Turn:
    return Turn(
        ts=time.time(),
        sender_number=sender_number,
        sender_name=sender_name,
        inbound=inbound,
        outbound=outbound,
        error=error,
        vault_context=vault_context,
        vault_candidates=vault_candidates,
    )


def render_for_prompt(turns: Iterable[Turn]) -> str:
    """Format a sequence of turns as a short transcript suitable for injection
    into a system prompt. Excludes errors; truncates long bodies."""
    lines: list[str] = []
    for t in turns:
        if t.error or not t.outbound:
            continue
        lines.append(f"[{t.sender_name}] {_truncate(t.inbound, 400)}")
        lines.append(f"[alice] {_truncate(t.outbound, 400)}")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
