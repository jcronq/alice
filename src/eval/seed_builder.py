"""Build the SEED labelled ground-truth set for the correctness harness.

Stratifies a sample off ``~/alice-mind/inner/state/speaking-turns.jsonl``
via :mod:`eval.sampling`, then applies a conservative, fully-documented
auto-labeller to each sampled turn. The output is a *seed* — a starting
point that REQUIRES HUMAN REVIEW, not a gold standard. The auto-labeller
errs toward "a reply was required" (every sampled turn historically got
one) and only sets ``expected_tools`` when the inbound carries an
unambiguous structured signal (a meal/workout/weight-change report).

Inbound text is redacted via :func:`eval.pii.redact` before being
written.

Label schema (one JSON object per line)::

    {
      "turn_id": str,
      "inbound": str,                 # PII-redacted
      "sender_name": str,
      "sampled_category": str,
      "expected_action_required": bool,
      "expected_tools": [str, ...],   # canonical names, best-guess
      "notes": str
    }

The first line of the file is a ``{"_seed_meta": true, ...}`` marker the
harness loader skips; it documents the seed/review caveat inline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from eval.pii import redact
from eval.sampling import (
    _annotate_turns,
    classify_turn,
    load_speaking_log,
    stratified_sample,
)

__all__ = ["auto_label", "build_seed", "main"]

# ~28-turn seed (small enough to hand-review, broad enough to stratify).
SEED_TARGETS: dict[str, int] = {
    "tactical": 8,
    "design": 6,
    "image": 3,
    "tool-heavy": 4,
    "conversational": 4,
    "edge": 3,
}

SEED_META = {
    "_seed_meta": True,
    "note": (
        "SEED labelled set — auto-labelled, REQUIRES HUMAN REVIEW. "
        "`expected_action_required` defaults True (every sampled turn "
        "historically got a reply); `expected_tools` is a best-guess "
        "from inbound keywords and is the knob Jason should review. Do "
        "not treat pass-rates over this set as ground truth until the "
        "labels are reviewed."
    ),
}

# --- auto-label keyword sets (documented judgment calls) -------------------

_MEAL_RE = re.compile(
    r"\b(ate|eaten|breakfast|lunch|dinner|snack|meal|"
    r"protein shake|yogurt|granola|calories|had (?:a|some|my))\b",
    re.IGNORECASE,
)
_WORKOUT_RE = re.compile(
    r"\b(workout|bench|squat|deadlift|ohp|overhead press|curl|row|"
    r"sets?|reps?|lifted|gym|\d+\s*x\s*\d+|crushed it|upper|lower)\b",
    re.IGNORECASE,
)
_WEIGHT_CHANGE_RE = re.compile(
    r"\b(bump|increase|raise|drop|lower|set)\b.*\b(to)\b.*\d|\b\d+\s*(kg|lb|lbs)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"\?\s*$|^\s*(what|when|where|who|why|how|can you|could you|"
    r"would you|is there|are there|do you|did you)\b",
    re.IGNORECASE,
)


def auto_label(turn: dict) -> dict[str, Any]:
    """Return the auto-label dict for one annotated sample turn.

    Conservative, keyword-driven. Every branch's reasoning lands in
    ``notes`` so a reviewer can see *why* the label was chosen.
    """
    inbound = turn.get("inbound") or ""
    category = turn.get("sampled_category") or classify_turn(turn)

    expected_tools: list[str] = []
    notes_parts: list[str] = []

    # Order matters: weight-change before workout (a "bump bench to 105"
    # is an update-weight request, not a workout log).
    if _WEIGHT_CHANGE_RE.search(inbound) and re.search(
        r"\b(bump|increase|raise|drop|lower|set)\b", inbound, re.IGNORECASE
    ):
        expected_tools = ["update-weight"]
        notes_parts.append("inbound looks like a lift-weight change request")
    elif _MEAL_RE.search(inbound):
        expected_tools = ["log-meal"]
        notes_parts.append("inbound mentions food/eating → meal log expected")
    elif _WORKOUT_RE.search(inbound):
        expected_tools = ["log-workout"]
        notes_parts.append("inbound mentions a lift/workout → workout log expected")
    elif _QUESTION_RE.search(inbound):
        notes_parts.append("direct question → reply (send_message) expected")
    else:
        notes_parts.append("conversational → reply (send_message) expected")

    # Default: a reply was required. Every sampled turn historically got
    # one, so action_required defaults True. This is the conservative
    # call flagged for review in the seed meta.
    expected_action_required = True
    notes_parts.append(
        "action_required defaulted True (historical turn had a reply) — REVIEW"
    )
    if expected_tools:
        notes_parts.append("expected_tools is a keyword best-guess — REVIEW")

    return {
        "turn_id": turn.get("turn_id"),
        "inbound": redact(inbound),
        "sender_name": turn.get("sender_name") or "",
        "sampled_category": category,
        "expected_action_required": expected_action_required,
        "expected_tools": expected_tools,
        "notes": "; ".join(notes_parts),
    }


def build_seed(
    *,
    log_path: str | Path,
    seed: int = 42,
    targets: dict[str, int] | None = None,
) -> list[dict]:
    """Sample + auto-label. Returns the list of label dicts (no meta)."""
    targets = targets or SEED_TARGETS
    rows = load_speaking_log(log_path)
    annotated = _annotate_turns(rows)
    sample = stratified_sample(annotated, targets, seed=seed)
    for t in sample:
        t["sampled_category"] = classify_turn(t)
    return [auto_label(t) for t in sample if t.get("inbound")]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="eval.seed_builder",
        description="Build the SEED labelled set for the correctness harness.",
    )
    p.add_argument(
        "--log",
        default="~/alice-mind/inner/state/speaking-turns.jsonl",
    )
    p.add_argument(
        "--out",
        default="configs/speaking_correctness_seed.jsonl",
    )
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    labels = build_seed(log_path=args.log, seed=args.seed)
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(SEED_META, ensure_ascii=False) + "\n")
        for row in labels:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        f"Wrote {len(labels)} labelled seed cases (+1 meta line) to {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
