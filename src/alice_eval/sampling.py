"""Stratified sample extraction for the speaking-quality eval.

Reads ``inner/state/speaking-turns.jsonl`` (or a path supplied via
``--log``), classifies each turn into one of six categories, groups
turns into conversations using the 300s gap rule, and produces a
stratified sample of 40 turns written to ``eval_sample.jsonl``.

Adapted from the code-on-paper in
``cortex-memory/research/2026-05-16-eval-sampling-design.md``. The
design assumed the JSONL carried ``tools`` / ``tool_count`` /
``history_turns`` / ``has_attachment`` fields; the live log only has
``ts``, ``sender_number``, ``sender_name``, ``inbound``, ``outbound``,
``error``. We synthesise the missing signal via:

- ``turn_id``     — ``turn_<int(ts*1000)>`` (millisecond-resolution
  surrogate).
- ``history_pos`` — position within the conversation it belongs to.
  Conversations are runs of consecutive turns sharing
  ``sender_number`` and separated by < ``CONVERSATION_GAP_SECONDS``.
- ``has_attachment`` — text-pattern heuristic over the inbound /
  outbound prose ("image"/"photo"/"screenshot"/"attached"/"the
  image", etc.).
- ``tool_count`` — text-pattern heuristic over the *outbound*
  looking for at least two distinct tool-class hits (cozyhem, gh,
  curl, ssh, signal-cli, git, ``$(...)``, multiple backtick
  command blocks).

When the 28-day lookback yields fewer than the 40-turn target we
widen to the entire log and log the chosen lookback to stderr — the
design says "never abort".
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

__all__ = [
    "CATEGORY_TARGETS",
    "CONVERSATION_GAP_SECONDS",
    "DEFAULT_LOG_PATH",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_SEED",
    "classify_turn",
    "group_into_conversations",
    "load_speaking_log",
    "main_sample",
    "stratified_sample",
]


# Path is /home/alice/alice-mind/inner/state/speaking-turns.jsonl on
# the live host; resolve at runtime via expanduser so test setups can
# override.
DEFAULT_LOG_PATH = Path("~/alice-mind/inner/state/speaking-turns.jsonl")
DEFAULT_OUTPUT_PATH = Path("eval_sample.jsonl")
DEFAULT_SEED = 42
DEFAULT_LOOKBACK_DAYS = 28
CONVERSATION_GAP_SECONDS = 300

CATEGORY_TARGETS: dict[str, int] = {
    "tactical": 10,
    "design": 10,
    "image": 5,
    "tool-heavy": 5,
    "conversational": 5,
    "edge": 5,
}

# Inbound/outbound markers for image-attachment turns. Match
# case-insensitively against the combined text.
_IMAGE_MARKERS = (
    "screenshot",
    "the image",
    "the screenshot",
    "the photo",
    "attached",
    "photo",
    "picture",
    " img ",
    "image of",
)

# Substrings that indicate the outbound side ran a tool. The
# tool-heavy classifier requires at least two distinct hits to
# avoid false positives on prose that merely *mentions* a tool.
_TOOL_MARKERS = (
    "cozyhem",
    "gh ",
    "gh pr",
    "gh issue",
    "curl ",
    "ssh ",
    "signal-cli",
    "git ",
    "Read(",
    "Bash(",
    "Grep(",
    "Edit(",
    "Write(",
    "mcp__",
)

_EDGE_MARKERS = (
    "haiku",
    "sorry",
    "i can't",
    "i won't",
    "you should",
    "be careful",
    "just let me know",
)


def _normalise_text(value: str | None) -> str:
    return (value or "").lower()


def _count_tool_markers(outbound: str) -> int:
    lower = outbound.lower()
    hits = {marker for marker in _TOOL_MARKERS if marker.lower() in lower}
    return len(hits)


def _looks_like_image_turn(inbound: str, outbound: str) -> bool:
    combined = (inbound + " " + outbound).lower()
    return any(marker in combined for marker in _IMAGE_MARKERS)


def classify_turn(turn: dict) -> str:
    """Return the eval category for ``turn``.

    Priority order matches the design:
    image > tool-heavy > design > edge > tactical > conversational.
    First match wins so a multi-turn tool call is categorised as
    tool-heavy, not design.
    """
    inbound = turn.get("inbound") or ""
    outbound = turn.get("outbound") or ""
    history_pos = int(turn.get("history_pos", 1))

    if _looks_like_image_turn(inbound, outbound):
        return "image"

    if _count_tool_markers(outbound) >= 2:
        return "tool-heavy"

    if history_pos > 5:
        return "design"

    combined_lower = _normalise_text(inbound) + " " + _normalise_text(outbound)
    if any(marker in combined_lower for marker in _EDGE_MARKERS):
        return "edge"

    if (
        _count_tool_markers(outbound) == 0
        and history_pos <= 2
        and len(inbound) < 200
    ):
        return "tactical"

    return "conversational"


def load_speaking_log(path: str | Path) -> list[dict]:
    """Read the JSONL log and return one dict per non-empty line.

    Skips lines that fail to parse with a stderr warning; we'd rather
    sample around a corrupt record than crash.
    """
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"speaking log not found: {resolved}")
    rows: list[dict] = []
    with resolved.open() as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                print(
                    f"WARNING: skipping {resolved}:{line_no} — {exc}",
                    file=sys.stderr,
                )
    return rows


def group_into_conversations(
    turns: Iterable[dict],
    gap_seconds: float = CONVERSATION_GAP_SECONDS,
) -> list[list[dict]]:
    """Group ``turns`` into conversations.

    Two consecutive turns from the same ``sender_number`` belong to
    the same conversation when their ``ts`` differ by less than
    ``gap_seconds``. A change of sender or a larger gap starts a new
    conversation. Turns must arrive in ``ts``-ascending order; we
    sort defensively.
    """
    ordered = sorted(
        (t for t in turns if t.get("ts") is not None),
        key=lambda t: float(t["ts"]),
    )
    conversations: list[list[dict]] = []
    current: list[dict] = []
    last_sender: str | None = None
    last_ts: float | None = None
    for turn in ordered:
        ts = float(turn["ts"])
        sender = turn.get("sender_number")
        new_conv = (
            not current
            or sender != last_sender
            or last_ts is None
            or (ts - last_ts) > gap_seconds
        )
        if new_conv:
            if current:
                conversations.append(current)
            current = []
        current.append(turn)
        last_sender = sender
        last_ts = ts
    if current:
        conversations.append(current)
    return conversations


def _annotate_turns(turns: list[dict]) -> list[dict]:
    """Mutate-and-return: add ``turn_id`` and ``history_pos`` to each
    turn based on conversation grouping.

    ``turn_id`` is deterministic given ``ts``. ``history_pos`` is the
    1-indexed position within the conversation, so a position > 5
    triggers the "design" category.
    """
    conversations = group_into_conversations(turns)
    by_id: dict[int, dict] = {}
    for conversation in conversations:
        for pos, turn in enumerate(conversation, 1):
            ts = float(turn["ts"])
            turn["turn_id"] = f"turn_{int(ts * 1000)}"
            turn["history_pos"] = pos
            # Conversation index keyed by id() for downstream history
            # reconstruction during replay. Not serialised.
            turn["_conversation_index"] = pos - 1
            turn["_conversation_id"] = conversation[0]["turn_id"]
            by_id[id(turn)] = turn
    return turns


def _filter_by_lookback(
    turns: list[dict], lookback_days: int | None
) -> tuple[list[dict], int]:
    """Return ``(filtered, lookback_used)``.

    If ``lookback_days`` is None or the filter yields < 40, fall back
    to the full set.
    """
    if not lookback_days:
        return turns, 0
    if not turns:
        return turns, lookback_days
    latest_ts = max(float(t["ts"]) for t in turns)
    cutoff_ts = latest_ts - lookback_days * 86400
    filtered = [t for t in turns if float(t["ts"]) >= cutoff_ts]
    return filtered, lookback_days


def stratified_sample(
    turns: list[dict],
    n_per_category: dict[str, int] | None = None,
    seed: int = DEFAULT_SEED,
) -> list[dict]:
    """Return a stratified sample of ``turns``.

    For each category in ``n_per_category`` we draw ``n`` turns at
    random; when a category is short we log a warning and top up
    from the ``conversational`` catch-all so the total still equals
    ``sum(n_per_category.values())`` when possible.
    """
    targets = n_per_category or CATEGORY_TARGETS
    rng = random.Random(seed)

    by_category: dict[str, list[dict]] = defaultdict(list)
    for turn in turns:
        by_category[classify_turn(turn)].append(turn)

    sample: list[dict] = []
    sampled_ids: set[str] = set()

    for category, requested in targets.items():
        pool = by_category.get(category, [])
        if len(pool) >= requested:
            picked = rng.sample(pool, requested)
        else:
            print(
                f"WARNING: '{category}' has {len(pool)} turns, "
                f"requested {requested} — taking all available",
                file=sys.stderr,
            )
            picked = list(pool)
        for turn in picked:
            sample.append(turn)
            sampled_ids.add(turn["turn_id"])

    # Shortfall makeup from the conversational catch-all (or any
    # remaining unused turns if conversational is also exhausted).
    expected_total = sum(targets.values())
    shortfall = expected_total - len(sample)
    if shortfall > 0:
        # Prefer the catch-all bucket; widen to "anything not yet
        # sampled" if that's still short.
        catch_all = [
            t for t in by_category.get("conversational", [])
            if t["turn_id"] not in sampled_ids
        ]
        if len(catch_all) < shortfall:
            extras = [
                t for cat_pool in by_category.values()
                for t in cat_pool
                if t["turn_id"] not in sampled_ids
                and t not in catch_all
            ]
            catch_all = catch_all + extras
        if catch_all:
            extra_count = min(shortfall, len(catch_all))
            extras = rng.sample(catch_all, extra_count)
            print(
                f"WARNING: backfilling {extra_count} turns from "
                f"catch-all to hit target of {expected_total}",
                file=sys.stderr,
            )
            for turn in extras:
                sample.append(turn)
                sampled_ids.add(turn["turn_id"])

    return sample


@dataclass(slots=True)
class SampleResult:
    """Container returned by :func:`main_sample` — handy for tests."""

    sample: list[dict]
    output_path: Path
    lookback_used: int
    total_turns: int


def main_sample(
    *,
    log_path: str | Path | None = None,
    out_path: str | Path | None = None,
    seed: int = DEFAULT_SEED,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    targets: dict[str, int] | None = None,
) -> SampleResult:
    """Run the sampler end-to-end and persist ``eval_sample.jsonl``.

    Public so callers (CLI and tests) share one code path.
    """
    log_path = Path(log_path or DEFAULT_LOG_PATH).expanduser()
    out_path = Path(out_path or DEFAULT_OUTPUT_PATH).expanduser()
    targets = targets or CATEGORY_TARGETS

    rows = load_speaking_log(log_path)
    annotated = _annotate_turns(rows)
    filtered, _ = _filter_by_lookback(annotated, lookback_days)
    lookback_used = lookback_days
    if len(filtered) < sum(targets.values()):
        print(
            f"WARNING: {lookback_days}-day lookback yielded only "
            f"{len(filtered)} turns; widening to full log "
            f"({len(annotated)} turns)",
            file=sys.stderr,
        )
        filtered = annotated
        lookback_used = 0

    sample = stratified_sample(filtered, targets, seed=seed)

    for turn in sample:
        turn["sampled_category"] = classify_turn(turn)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for turn in sample:
            payload = {k: v for k, v in turn.items() if not k.startswith("_")}
            fh.write(json.dumps(payload) + "\n")

    print(
        f"Sampled {len(sample)} turns from {len(filtered)} candidate "
        f"turns (lookback_days={lookback_used or 'all'}); wrote {out_path}",
        file=sys.stderr,
    )

    return SampleResult(
        sample=sample,
        output_path=out_path,
        lookback_used=lookback_used,
        total_turns=len(filtered),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice_eval.sampling",
        description="Stratified sampler for the speaking-quality eval.",
    )
    parser.add_argument(
        "--log",
        type=str,
        default=str(DEFAULT_LOG_PATH),
        help="Path to speaking-turns.jsonl",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output path for the sampled JSONL",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="Restrict to turns whose ts is within N days of the "
        "log's most recent turn; widens to full log on shortfall.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    main_sample(
        log_path=args.log,
        out_path=args.out,
        seed=args.seed,
        lookback_days=args.lookback_days,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
