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
from typing import Iterable, Optional


@dataclass
class Turn:
    ts: float  # unix seconds, local clock
    sender_number: str
    sender_name: str
    inbound: str
    outbound: Optional[str]  # None when we didn't reply (error, empty, skipped)
    error: Optional[str] = None


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
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            out.append(Turn(**obj))
        return out


def new_turn(
    sender_number: str,
    sender_name: str,
    inbound: str,
    outbound: Optional[str] = None,
    error: Optional[str] = None,
) -> Turn:
    return Turn(
        ts=time.time(),
        sender_number=sender_number,
        sender_name=sender_name,
        inbound=inbound,
        outbound=outbound,
        error=error,
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
