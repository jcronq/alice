"""Turn-the-sample-into-instances pipeline.

Reads ``eval_sample.jsonl`` (produced by :mod:`eval.sampling`),
inspects each turn's historical artifact (the outbound text from the
speaking log), derives the assertion file per the design's
"Per-category assertions" section, and writes one
``instances/<turn_id>.assert.json`` per sampled turn.

Design reference:
``cortex-memory/research/2026-05-18-speaking-benchmark-design.md``.

Heuristics
----------

The speaking log only carries the *final* outbound prose, not
structured tool_use blocks. We extract tool calls by regex over the
outbound text and treat the recognised tool surface as the ground
truth. This is intentionally lossy — it captures the dominant
historical tool surface (``send_message``, ``cozyhem``, ``signal-cli``
fallback) plus the canonical skill names — and skips the rare
multi-tool combos that the assertion runner's set-match tolerates
anyway.

Channel inference: ``cli`` for the small number of historical turns
whose ``sender_number`` is null or whose outbound mentions ``$`` or
``Bash(``; everything else is ``signal``.

Each derived assertion file ends up with:

- 2-4 ``pass_to_pass`` regression guards (no forbidden tool, channel
  format, no empty reply, no hallucinated tool)
- 1-3 ``fail_to_pass`` positive expectations whose shape depends on
  the sampled category

Turns whose category provides no checkable positive expectation
(``tactical``, ``conversational``, ``edge`` without tool surface) get
the loose ``bleu_threshold`` assertion against the historical reply
so that catastrophic regressions still trip.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from eval.assertions import AssertionFile, extract_tool_names

__all__ = [
    "DEFAULT_BLEU_THRESHOLD",
    "DEFAULT_INSTANCES_DIR",
    "DEFAULT_KNOWN_TOOLS",
    "derive_assertion_file",
    "main_instances",
    "write_assertion_files",
]

log = logging.getLogger(__name__)

DEFAULT_INSTANCES_DIR = Path("instances")
DEFAULT_BLEU_THRESHOLD = 0.15

# Tool surface Alice canonically has access to. The
# ``no_hallucinated_tool`` assertion uses this as the allow-list.
DEFAULT_KNOWN_TOOLS: tuple[str, ...] = (
    # MCP / Alice
    "send_message",
    "append_note",
    "schedule",
    "log_event",
    # Skills
    "log-meal",
    "log-workout",
    "update-weight",
    "log-journal",
    "cortex-memory",
    "init",
    "review",
    "security-review",
    # CLI / system tools that show up in the historical log
    "cozyhem",
    "curl",
    "ssh",
    "gh",
    "git",
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "Agent",
    "Task",
)


# Re-used from the sampler classifier, but defined locally so we
# don't import private helpers across modules.
_SKILL_NAMES = (
    "log-meal",
    "log-workout",
    "update-weight",
    "log-journal",
    "cortex-memory",
)

_TOOL_HEAVY_TOKENS = (
    "cozyhem",
    "curl ",
    "ssh ",
    "signal-cli",
    "gh pr",
    "gh issue",
    "Bash(",
    "Read(",
    "Edit(",
    "Write(",
    "Grep(",
)

_DISPATCH_TOKENS = (
    "Agent(",
    "Task(",
    "dispatch_worker",
    "spawn worker",
    "spawning a worker",
    "worker dispatched",
)


def _infer_channel(turn: dict) -> str:
    sender = turn.get("sender_number")
    outbound = turn.get("outbound") or ""
    if not sender:
        return "cli"
    if "$ " in outbound or "Bash(" in outbound:
        # Definitely a CLI-style turn even if the user came in via Signal.
        return "cli"
    return "signal"


def _detect_skill(outbound: str) -> str | None:
    lower = outbound.lower()
    for skill in _SKILL_NAMES:
        if skill in lower:
            return skill
    return None


def _detect_routing(outbound: str) -> str:
    return "dispatch" if any(t in outbound for t in _DISPATCH_TOKENS) else "inline"


_RECIPIENT_RE = re.compile(
    r"recipient\s*=\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)')",
    re.IGNORECASE,
)


def _detect_send_message_recipient(outbound: str) -> str | None:
    match = _RECIPIENT_RE.search(outbound)
    if not match:
        return None
    return match.group("dq") or match.group("sq")


_NOUN_PHRASE_RE = re.compile(r"\b[a-z][a-z0-9_-]{3,}\b")
_STOPWORDS = {
    "this", "that", "with", "from", "have", "your", "into", "about",
    "would", "could", "should", "they", "them", "what", "when", "where",
    "their", "there", "which", "these", "those", "looks", "look", "like",
    "just", "going", "going", "still", "want", "wants", "need", "needs",
    "make", "made", "make", "thing", "things", "good", "bad", "really",
    "actually", "right", "much", "more", "most", "less", "even", "kind",
    "very", "some", "also", "back", "after", "before", "first", "last",
    "next", "again", "then", "well", "yeah", "okay", "sure", "today",
    "tonight", "tomorrow", "yesterday", "evening", "morning", "afternoon",
}


def _extract_entities_from_reply(reply: str, *, max_entities: int = 8) -> list[str]:
    seen: list[str] = []
    for token in _NOUN_PHRASE_RE.findall(reply.lower()):
        if token in _STOPWORDS:
            continue
        if token in seen:
            continue
        seen.append(token)
        if len(seen) >= max_entities:
            break
    return seen


def _detect_skill_fields(outbound: str, skill: str) -> dict[str, Any]:
    """Best-effort extraction of canonical skill-fire fields from the
    historical outbound. We capture the obvious numerics ("250 cal",
    "32g protein", "100kg bench") plus any quoted/clear meal/exercise
    name. Returns ``{}`` if nothing checkable.
    """
    lower = outbound.lower()
    fields: dict[str, Any] = {}

    if skill == "log-meal":
        # Number-then-label ("320 cal", "12g protein") AND label-then-number
        # ("calories: 320", "protein: 12g") both occur in the historical log.
        cal = re.search(
            r"(?:([0-9]+(?:\.[0-9]+)?)\s*(?:k?cal|calories)"
            r"|(?:k?cal|calories)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?))",
            lower,
        )
        if cal:
            fields["calories"] = float(cal.group(1) or cal.group(2))
        protein = re.search(
            r"(?:([0-9]+(?:\.[0-9]+)?)\s*g?\s*protein"
            r"|protein\s*[:=]\s*([0-9]+(?:\.[0-9]+)?))",
            lower,
        )
        if protein:
            fields["protein"] = float(protein.group(1) or protein.group(2))

    if skill == "log-workout":
        weight = re.search(
            r"([0-9]+(?:\.[0-9]+)?)\s*(kg|lb|lbs)\s*(?:x|×|for)?\s*([0-9]+)?",
            lower,
        )
        if weight:
            fields["weight"] = float(weight.group(1))

    if skill == "update-weight":
        target = re.search(
            r"(?:to|→|->)\s*([0-9]+(?:\.[0-9]+)?)\s*(kg|lb|lbs)?",
            lower,
        )
        if target:
            fields["new_weight"] = float(target.group(1))

    return fields


def derive_assertion_file(
    turn: dict,
    *,
    known_tools: Sequence[str] = DEFAULT_KNOWN_TOOLS,
    bleu_threshold: float = DEFAULT_BLEU_THRESHOLD,
) -> AssertionFile:
    """Derive the ``AssertionFile`` for a sampled turn.

    The turn dict comes from ``eval_sample.jsonl`` and is expected to
    carry at minimum ``turn_id``, ``sampled_category``, ``inbound``,
    ``outbound``.
    """
    turn_id = turn.get("turn_id") or "turn_unknown"
    category = (
        turn.get("sampled_category")
        or turn.get("category")
        or "unknown"
    )
    outbound = turn.get("outbound") or ""
    channel = _infer_channel(turn)

    pass_to_pass: list[dict[str, Any]] = [
        {"type": "no_empty_reply"},
        {"type": "channel_format_ok", "channel": channel},
        {
            "type": "no_hallucinated_tool",
            "allowed_tools": list(known_tools),
        },
    ]
    # On Signal channels with a send_message-shaped outbound we add the
    # forbidden-tool guard: don't shell out to signal-cli when
    # send_message would do.
    if channel == "signal":
        pass_to_pass.append(
            {
                "type": "no_forbidden_tool",
                "tool": "signal-cli",
                "available_alt": "send_message",
            }
        )

    fail_to_pass: list[dict[str, Any]] = []
    historical_tool_calls: list[dict[str, Any]] = []

    observed_tools = extract_tool_names(outbound)

    # Skill-fire: highest priority since it's a strong positive signal.
    skill = _detect_skill(outbound)
    if skill:
        skill_fields = _detect_skill_fields(outbound, skill)
        fail_to_pass.append(
            {
                "type": "skill_invocation",
                "skill": skill,
                "required_fields": skill_fields,
            }
        )
        historical_tool_calls.append(
            {"name": skill, "args": skill_fields, "kind": "skill"}
        )

    # Tool-heavy / tool-call-bearing turns
    if observed_tools and any(t in outbound for t in _TOOL_HEAVY_TOKENS):
        meaningful = [
            t
            for t in observed_tools
            if t in set(known_tools) or t in {"signal-cli"}
        ]
        if meaningful:
            fail_to_pass.append(
                {
                    "type": "tool_call_match",
                    "expected_tools": meaningful,
                    "match": "set",
                }
            )
            historical_tool_calls.extend(
                {"name": t, "args": {}, "kind": "tool"} for t in meaningful
            )

    # send_message recipient assertion when historically present
    recipient = _detect_send_message_recipient(outbound)
    if recipient:
        fail_to_pass.append(
            {
                "type": "tool_call_match",
                "expected_tools": ["send_message"],
                "match": "set",
            }
        )
        fail_to_pass.append(
            {
                "type": "arg_match",
                "tool": "send_message",
                "arg": "recipient",
                "value": recipient,
                "strategy": "exact",
            }
        )
        # And a loose message-body Jaccard against the outbound prose.
        # We strip the send_message(...) wrapper-ish noise; the
        # extracted prose is the historical reply text.
        fail_to_pass.append(
            {
                "type": "arg_match",
                "tool": "send_message",
                "arg": "message",
                "value": _strip_tool_wrappers(outbound)[:500],
                "strategy": "jaccard",
                "threshold": 0.30,
            }
        )

    # Routing decision — only assert when we can read a clear dispatch
    # signal from the historical outbound.
    routing = _detect_routing(outbound)
    if routing == "dispatch":
        fail_to_pass.append(
            {"type": "routing_decision", "expected": "dispatch"}
        )

    # Image / multimodal: derive entity overlap.
    if category == "image":
        entities = _extract_entities_from_reply(outbound)
        if entities:
            fail_to_pass.append(
                {
                    "type": "entity_overlap",
                    "entities": entities,
                    "min_overlap": 0.5,
                    # Loose threshold — the design says 0.8 but the
                    # entity extractor is noun-phrase, not VLM; 0.5
                    # catches catastrophic misses without false
                    # positives. Tighten when VLM entity extraction
                    # ships (open question #2 in the design).
                }
            )

    # Prose-only fallback: if nothing checkable yet, BLEU against the
    # historical reply.
    if not fail_to_pass:
        fail_to_pass.append(
            {
                "type": "bleu_threshold",
                "reference": _strip_tool_wrappers(outbound),
                "min_bleu": bleu_threshold,
            }
        )

    return AssertionFile(
        turn_id=turn_id,
        category=category,
        channel=channel,
        pass_to_pass=pass_to_pass,
        fail_to_pass=fail_to_pass,
        historical_reply=outbound,
        historical_tool_calls=historical_tool_calls,
    )


_TOOL_WRAPPER_RE = re.compile(
    r"\b(send_message|append_note|log_event|schedule)\s*\([^)]*\)",
    re.DOTALL,
)


def _strip_tool_wrappers(text: str) -> str:
    """Remove obvious tool-call wrappers so a BLEU comparison sees the
    actual prose, not ``send_message(...)`` boilerplate."""
    inner_re = re.compile(
        r"""\b(?:send_message|append_note|log_event|schedule)
            \s*\(\s*[^)]*?
            message\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')""",
        re.VERBOSE | re.DOTALL,
    )
    parts: list[str] = []
    for match in inner_re.finditer(text):
        parts.append(match.group("dq") or match.group("sq") or "")
    if parts:
        return " ".join(parts)
    # Fall back to stripping the wrappers only.
    return _TOOL_WRAPPER_RE.sub("", text).strip() or text


# ---------------------------------------------------------------------------
# I/O


def write_assertion_files(
    samples: Iterable[dict],
    out_dir: str | Path = DEFAULT_INSTANCES_DIR,
    *,
    known_tools: Sequence[str] = DEFAULT_KNOWN_TOOLS,
    bleu_threshold: float = DEFAULT_BLEU_THRESHOLD,
) -> list[Path]:
    """Write one assertion file per sample. Returns the paths written."""
    base = Path(out_dir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sample in samples:
        af = derive_assertion_file(
            sample, known_tools=known_tools, bleu_threshold=bleu_threshold
        )
        path = base / f"{af.turn_id}.assert.json"
        path.write_text(
            json.dumps(af.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        written.append(path)
    return written


def load_samples(path: str | Path) -> list[dict]:
    resolved = Path(path).expanduser()
    rows: list[dict] = []
    with resolved.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def main_instances(
    *,
    sample_path: str | Path,
    out_dir: str | Path = DEFAULT_INSTANCES_DIR,
    bleu_threshold: float = DEFAULT_BLEU_THRESHOLD,
) -> list[Path]:
    samples = load_samples(sample_path)
    paths = write_assertion_files(
        samples, out_dir=out_dir, bleu_threshold=bleu_threshold
    )
    print(
        f"Wrote {len(paths)} assertion files to {Path(out_dir).expanduser()}",
        file=sys.stderr,
    )
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval.instances",
        description="Derive per-instance assertion files from a sample.",
    )
    p.add_argument(
        "--sample",
        default="eval_sample.jsonl",
        help="Path to the eval_sample.jsonl produced by `eval sample`",
    )
    p.add_argument(
        "--out-dir",
        default=str(DEFAULT_INSTANCES_DIR),
        help="Directory to write <turn_id>.assert.json files",
    )
    p.add_argument(
        "--bleu-threshold",
        type=float,
        default=DEFAULT_BLEU_THRESHOLD,
        help="BLEU-4 floor for prose-only turns",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    main_instances(
        sample_path=args.sample,
        out_dir=args.out_dir,
        bleu_threshold=args.bleu_threshold,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
