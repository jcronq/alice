"""Persistent per-turn log for speaking Alice.

One JSONL line per processed message. Read on startup to rebuild the recent
working context and survive restarts amnesia-free. The signal-cli log is the
source of truth for *envelopes*; this file is the record of *Alice's turns*.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional


# How many days of turn history to retain. Older entries are pruned on
# append. Bootstrap/summary readers (``turn_runner.py``) only ever pull
# ``tail(5)``/``tail(20)``, so a 30-day floor is plenty.
RETENTION_DAYS = 30
RETENTION_SECONDS = RETENTION_DAYS * 86400

# Per-field truncation ceiling. Compaction turns inject the full
# context summary into ``inbound``/``outbound``, which inflates entries
# past 10 KB. The full summary already persists at
# ``inner/state/context-summary.md``, so the log only needs a marker.
MAX_FIELD_BYTES = 4096
TRUNCATION_MARKER = "...[truncated]"

# Prune-rate gate. Re-reading + rewriting the whole file on every
# append would be wasteful — but skipping prune indefinitely is what
# got us to 4.4 MB in the first place. We prune opportunistically when
# the file is already heavier than this threshold. At ~55 turns/day and
# ~2.4 KB/turn the file hits 1 MB in ~7 days, which keeps the prune
# cadence weekly-ish without per-write scans on a fresh file.
PRUNE_SIZE_THRESHOLD_BYTES = 1_000_000


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
        payload = _truncate_fields(asdict(turn))
        with self.path.open("a") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # Opportunistic prune — only when the file is heavy enough that
        # a full rewrite is worth doing. Cheap stat() on every write,
        # full scan + rewrite roughly weekly under normal traffic.
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size >= PRUNE_SIZE_THRESHOLD_BYTES:
            self._prune_old_entries()

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

    def _prune_old_entries(self) -> None:
        """Rewrite the log keeping only entries with ``ts >= now - 30d``.

        Atomic ``.tmp`` sibling + ``os.replace`` so a crash mid-prune
        never leaves a partial file. Lines that fail to parse are kept
        as-is (better to retain noise than to silently discard data).
        Lines with no ``ts`` field are also kept — we can't judge
        their age.
        """
        if not self.path.is_file():
            return
        cutoff = time.time() - RETENTION_SECONDS
        kept: list[str] = []
        dropped = 0
        with self.path.open("r") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    kept.append(stripped)
                    continue
                ts = obj.get("ts")
                if isinstance(ts, (int, float)) and ts < cutoff:
                    dropped += 1
                    continue
                kept.append(stripped)
        if dropped == 0:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
        os.replace(tmp, self.path)


def _truncate_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Cap any string field at ``MAX_FIELD_BYTES``.

    The 4 KB ceiling targets compaction turns whose ``inbound`` /
    ``outbound`` embed the full vault summary. The summary is the
    authoritative copy at ``inner/state/context-summary.md`` — the
    turn log only needs enough text to show what happened.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > MAX_FIELD_BYTES:
            out[key] = value[: MAX_FIELD_BYTES - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        else:
            out[key] = value
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
