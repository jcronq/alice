"""Pull *candidate* turns out of ``speaking-turns.jsonl`` for hand-labelling.

This is a SOURCING tool, not a labeller. It surfaces the three populations
the speaking-harness correctness eval cares about so a human can apply the
labelling rubric (see
``~/alice-mind/inner/state/speaking-harness-eval-labels.jsonl`` and the
task brief):

- ``emoji_only``   — outbound is a bare reaction/emoji (the bare-ack mode).
- ``empty``        — outbound is null/empty (the missing-send mode); CLI
                     turns with action-implying inbounds are the interesting
                     subset here.
- ``control``      — substantive inbound → substantive outbound (the
                     clearly-correct controls).

Channel is derived from ``sender_number``: a real E.164 phone number is a
Signal turn; anything else (a dev/cli/worker pseudo-id, or absent) is a CLI
turn. The labels themselves are NOT written here — emit candidates and let
the rubric decide ``action_required`` / ``acceptable_ack_only`` /
``expected_tools`` per turn.

Usage::

    python -m eval.label_extractor --bucket emoji_only
    python -m eval.label_extractor --bucket empty --channel cli --limit 60
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "classify_channel",
    "is_emoji_only",
    "is_empty",
    "load_turns",
    "make_turn_id",
]

DEFAULT_LOG = "~/alice-mind/inner/state/speaking-turns.jsonl"

# E.164: leading '+' then 8-15 digits. Signal senders are real phone numbers;
# CLI/dev/worker senders are pseudo-ids ("cli", "dev-...", "worker", "") that
# don't match.
_E164_RE = re.compile(r"^\+\d{8,15}$")

# Any ASCII letter or digit => the text carries substantive content, so it is
# not a bare emoji/reaction reply.
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def classify_channel(sender_number: Any) -> str:
    """Return ``"signal"`` for a real E.164 phone number, else ``"cli"``."""
    if isinstance(sender_number, str) and _E164_RE.match(sender_number.strip()):
        return "signal"
    return "cli"


def is_empty(outbound: Any) -> bool:
    """True iff there was effectively no reply text."""
    return not (isinstance(outbound, str) and outbound.strip())


def is_emoji_only(outbound: Any) -> bool:
    """True iff the reply is non-empty but contains no alphanumeric chars.

    Catches bare reactions like ``👍``, ``❤️``, ``😊``, ``👍👍`` and
    emoji+punctuation (``👍.``) — the bare-ack failure-mode surface.
    """
    if is_empty(outbound):
        return False
    return _ALNUM_RE.search(outbound) is None


def make_turn_id(turn: dict) -> str:
    """Stable id from the timestamp (matches the daemon's ``turn_<ms>``)."""
    ts = turn.get("ts")
    try:
        return f"turn_{int(float(ts) * 1000)}"
    except (TypeError, ValueError):
        return "turn_unknown"


def load_turns(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).expanduser().open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _candidate(turn: dict, bucket: str) -> dict:
    return {
        "turn_id": make_turn_id(turn),
        "ts": turn.get("ts"),
        "channel": classify_channel(turn.get("sender_number")),
        "sender_name": turn.get("sender_name"),
        "bucket": bucket,
        "inbound": turn.get("inbound") or "",
        "historical_outbound": turn.get("outbound") or "",
    }


def extract(
    turns: list[dict],
    *,
    bucket: str,
    channel: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    out: list[dict] = []
    for turn in turns:
        ob = turn.get("outbound")
        if bucket == "emoji_only" and not is_emoji_only(ob):
            continue
        if bucket == "empty" and not is_empty(ob):
            continue
        if bucket == "control" and (is_empty(ob) or is_emoji_only(ob)):
            continue
        if bucket == "control" and not (turn.get("inbound") or "").strip():
            continue
        cand = _candidate(turn, bucket)
        if channel and cand["channel"] != channel:
            continue
        out.append(cand)
        if limit and len(out) >= limit:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eval.label_extractor")
    p.add_argument("--log", default=DEFAULT_LOG)
    p.add_argument(
        "--bucket",
        choices=["emoji_only", "empty", "control"],
        required=True,
    )
    p.add_argument("--channel", choices=["signal", "cli"], default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    turns = load_turns(args.log)
    cands = extract(
        turns, bucket=args.bucket, channel=args.channel, limit=args.limit
    )
    for c in cands:
        print(json.dumps(c, ensure_ascii=False))
    print(f"# {len(cands)} {args.bucket} candidates", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
