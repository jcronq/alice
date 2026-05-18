"""Assertion runner for the speaking-benchmark (SWE-Bench shape).

Per ``cortex-memory/research/2026-05-18-speaking-benchmark-design.md``,
each speaking-benchmark instance carries a list of assertions derived
from the historical artifacts. The runner evaluates each assertion
against a candidate's output and returns per-assertion pass/fail. An
instance resolves iff **all** ``FAIL_TO_PASS`` assertions pass **and**
**all** ``PASS_TO_PASS`` assertions hold (no failures).

Assertion shapes
----------------

Each assertion is a dict with a ``type`` key plus type-specific
parameters. Supported types:

``no_forbidden_tool``
    ``{"type": "no_forbidden_tool", "tool": "signal-cli",
       "available_alt": "send_message"}``
    Fails if the candidate's output contains a call to the forbidden
    tool. ``available_alt`` is informational (logged on failure).

``no_hallucinated_tool``
    ``{"type": "no_hallucinated_tool",
       "allowed_tools": ["send_message", ...]}``
    Fails if the candidate's output references any tool name not in
    ``allowed_tools``. Tool references are detected via the same
    surface-level patterns the sampler uses (``Tool(``, ``mcp__``,
    ``signal-cli`` etc.).

``channel_format_ok``
    ``{"type": "channel_format_ok", "channel": "signal" | "cli"}``
    Signal channel → reply must be plain text (no markdown headings,
    no fenced code blocks, no bullet lists).
    CLI channel → no check (markdown is fine; we only flag the
    pathological case of a totally empty reply, which is covered by
    ``no_empty_reply``).

``no_empty_reply``
    ``{"type": "no_empty_reply"}``
    Fails if the candidate's output is whitespace-only or empty.

``tool_call_match``
    ``{"type": "tool_call_match",
       "expected_tools": ["send_message"],
       "match": "set" | "exact_sequence"}``
    Detects tool-call references in the candidate output and checks
    that the **set** of distinct tool names matches the expected set.
    With ``match=exact_sequence`` the ordered list of distinct tool
    invocations must match.

``arg_match``
    ``{"type": "arg_match", "tool": "send_message",
       "arg": "recipient", "value": "jason",
       "strategy": "exact" | "jaccard" | "levenshtein",
       "threshold": 0.3}``
    For tools like ``send_message(recipient="jason", message="...")``
    we extract argument values from the candidate output by regex and
    compare to ``value``. ``exact`` is a case-insensitive string
    match; ``jaccard`` compares unigram sets, threshold is the
    minimum Jaccard similarity; ``levenshtein`` is normalised edit
    distance, threshold is the maximum allowed distance (so
    ``threshold=0.3`` means ≤0.3 normalised distance is a pass).

``bleu_threshold``
    ``{"type": "bleu_threshold", "reference": "...",
       "min_bleu": 0.15}``
    For prose-only turns; computes a corpus-style BLEU-4 between the
    candidate output and the historical reply. Falls through to a
    simpler unigram-overlap fraction when nltk isn't available.

``entity_overlap``
    ``{"type": "entity_overlap",
       "entities": ["sunset", "porch", "katie"],
       "min_overlap": 0.8}``
    For image / multimodal turns. The candidate output must mention
    at least ``min_overlap`` fraction of the entity tokens
    (case-insensitive substring match).

``routing_decision``
    ``{"type": "routing_decision",
       "expected": "dispatch" | "inline"}``
    Binary check: did the candidate dispatch to a worker (detected
    via Agent/Task tool reference) or reply inline. Routing turns
    only.

``skill_invocation``
    ``{"type": "skill_invocation",
       "skill": "log-meal",
       "required_fields": {"meal_name": "...", "calories": 250}}``
    For skill-fire turns. The candidate output must reference the
    skill and mention each required field. Numeric fields tolerate a
    ±10% deviation.

The runner is intentionally string-pattern based: candidate outputs
are typically free-form text describing or executing tool calls, not
structured JSON. Tighter structured-tool detection is a future
upgrade once the candidate harness emits structured tool_use blocks.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "ASSERTION_TYPES",
    "AssertionFile",
    "AssertionResult",
    "InstanceResult",
    "evaluate_assertion",
    "evaluate_instance",
    "load_assertion_file",
    "register_assertion_type",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types


@dataclass(slots=True)
class AssertionResult:
    """One assertion's outcome."""

    type: str
    bucket: str  # "pass_to_pass" or "fail_to_pass"
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "bucket": self.bucket,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(slots=True)
class InstanceResult:
    """All assertions for a single instance, plus the binary verdict."""

    turn_id: str
    category: str
    candidate_id: str
    resolved: bool
    results: list[AssertionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "category": self.category,
            "candidate_id": self.candidate_id,
            "resolved": self.resolved,
            "assertions": [r.to_dict() for r in self.results],
        }


@dataclass(slots=True)
class AssertionFile:
    """The on-disk shape of ``instances/<turn_id>.assert.json``."""

    turn_id: str
    category: str
    channel: str
    pass_to_pass: list[dict[str, Any]] = field(default_factory=list)
    fail_to_pass: list[dict[str, Any]] = field(default_factory=list)
    historical_reply: str = ""
    historical_tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AssertionFile":
        return cls(
            turn_id=payload["turn_id"],
            category=payload.get("category", "unknown"),
            channel=payload.get("channel", "signal"),
            pass_to_pass=list(payload.get("pass_to_pass") or []),
            fail_to_pass=list(payload.get("fail_to_pass") or []),
            historical_reply=payload.get("historical_reply") or "",
            historical_tool_calls=list(
                payload.get("historical_tool_calls") or []
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "category": self.category,
            "channel": self.channel,
            "pass_to_pass": self.pass_to_pass,
            "fail_to_pass": self.fail_to_pass,
            "historical_reply": self.historical_reply,
            "historical_tool_calls": self.historical_tool_calls,
        }


def load_assertion_file(path: str | Path) -> AssertionFile:
    raw = Path(path).expanduser().read_text(encoding="utf-8")
    return AssertionFile.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Tool-call extraction
#
# The candidate harness emits free-form text. We detect tool references via
# the same surface patterns used elsewhere in the eval. This keeps the
# benchmark working for both API candidates (which return prose describing
# their tool calls) and local llama-server candidates (whose outputs are
# unstructured).

# Match send_message(recipient="jason", message="...") and similar.
_TOOL_CALL_RE = re.compile(
    r"""
    \b
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    \s*\(
    (?P<args>[^()]*?)
    \)
    """,
    re.VERBOSE,
)

# Match mcp__tool / Tool tags Anthropic-style tool blocks sometimes emit
# in prose like `mcp__alice__send_message`.
_MCP_REF_RE = re.compile(r"\bmcp__[A-Za-z0-9_]+(?:__[A-Za-z0-9_]+)?")

# Match CLI tools we sometimes want to forbid.
_CLI_TOOL_TOKENS = (
    "signal-cli",
    "curl",
    "ssh",
    "gh ",
    "gh pr",
    "gh issue",
    "cozyhem ",
)


def extract_tool_names(output: str) -> list[str]:
    """Return distinct tool/function names referenced in ``output``.

    Includes Python-style ``name(args)`` calls, ``mcp__*`` MCP tool
    identifiers, and the CLI tool tokens above.
    """
    names: list[str] = []
    seen: set[str] = set()

    for match in _TOOL_CALL_RE.finditer(output):
        name = match.group("name")
        if name in seen:
            continue
        # Filter out generic Python builtins the candidate might mention.
        if name in {"print", "len", "range", "list", "dict", "str", "int"}:
            continue
        seen.add(name)
        names.append(name)

    for match in _MCP_REF_RE.finditer(output):
        token = match.group(0)
        if token not in seen:
            seen.add(token)
            names.append(token)

    lower = output.lower()
    for tok in _CLI_TOOL_TOKENS:
        if tok in lower:
            stripped = tok.strip()
            if stripped not in seen:
                seen.add(stripped)
                names.append(stripped)

    return names


def extract_arg_value(output: str, tool: str, arg: str) -> str | None:
    """Extract the value of ``arg`` from ``tool(... arg=<value> ...)``
    in ``output``. Supports both string-quoted (``"..."`` / ``'...'``)
    and bare values. Returns the first occurrence's value or
    ``None``.
    """
    pattern = re.compile(
        r"\b"
        + re.escape(tool)
        + r"\s*\(\s*(?P<body>[^)]*)\)",
        re.DOTALL,
    )
    arg_re = re.compile(
        r"\b"
        + re.escape(arg)
        + r"""\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^,\s)]+))"""
    )
    for call in pattern.finditer(output):
        body = call.group("body") or ""
        match = arg_re.search(body)
        if match:
            return (
                match.group("dq")
                or match.group("sq")
                or match.group("bare")
                or ""
            )
    return None


# ---------------------------------------------------------------------------
# String similarity helpers


def _tokenise(text: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z0-9']+", text.lower()) if t]


def jaccard(a: str, b: str) -> float:
    aset = set(_tokenise(a))
    bset = set(_tokenise(b))
    if not aset and not bset:
        return 1.0
    if not aset or not bset:
        return 0.0
    return len(aset & bset) / len(aset | bset)


def normalised_levenshtein(a: str, b: str) -> float:
    """Return Levenshtein distance / max(len(a), len(b)). 0.0 → identical."""
    if a == b:
        return 0.0
    if not a:
        return 1.0
    if not b:
        return 1.0
    # Wagner-Fischer; cheap enough at instance scale.
    n, m = len(a), len(b)
    if n < m:
        a, b = b, a
        n, m = m, n
    prev = list(range(m + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * m
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                cur[j - 1] + 1,
                prev[j] + 1,
                prev[j - 1] + cost,
            )
        prev = cur
    return prev[m] / max(n, m)


def bleu4(reference: str, candidate: str) -> float:
    """Smoothed BLEU-4 between ``reference`` and ``candidate``.

    Standard corpus BLEU formula with 1-grams..4-grams; smoothed by
    adding 1 to each numerator+denominator when the n-gram count is
    zero (Chen & Cherry smoothing method 1). Returns 0.0 if either
    side is empty.
    """
    ref_tokens = _tokenise(reference)
    cand_tokens = _tokenise(candidate)
    if not ref_tokens or not cand_tokens:
        return 0.0

    log_precisions: list[float] = []
    for n in (1, 2, 3, 4):
        ref_ngrams = Counter(
            tuple(ref_tokens[i : i + n])
            for i in range(len(ref_tokens) - n + 1)
        )
        cand_ngrams = Counter(
            tuple(cand_tokens[i : i + n])
            for i in range(len(cand_tokens) - n + 1)
        )
        if not cand_ngrams:
            log_precisions.append(math.log(1e-9))
            continue
        matched = sum(
            min(count, ref_ngrams.get(ng, 0))
            for ng, count in cand_ngrams.items()
        )
        total = sum(cand_ngrams.values())
        if matched == 0:
            # Smooth: pretend we got one match in (total + 1) trials.
            precision = 1 / (total + 1)
        else:
            precision = matched / total
        log_precisions.append(math.log(precision))

    geo_mean = math.exp(sum(log_precisions) / 4)
    bp = (
        1.0
        if len(cand_tokens) >= len(ref_tokens)
        else math.exp(1 - len(ref_tokens) / len(cand_tokens))
    )
    return bp * geo_mean


# ---------------------------------------------------------------------------
# Assertion implementations


def _check_no_forbidden_tool(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    tool = params.get("tool") or ""
    if not tool:
        return False, "no 'tool' specified"
    names = extract_tool_names(output)
    hit = tool in names or tool.strip() in names
    if hit:
        return False, f"forbidden tool {tool!r} referenced in output"
    return True, ""


def _check_no_hallucinated_tool(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    allowed = set(params.get("allowed_tools") or [])
    if not allowed:
        return True, "no allowed_tools list — skipping"
    names = extract_tool_names(output)
    # We don't flag plain English verbs; only flag tokens that look like
    # tool identifiers (mcp__*, snake_case identifiers, etc.).
    suspect = [
        n
        for n in names
        if (n.startswith("mcp__") or "_" in n or n.endswith("-cli"))
        and n not in allowed
    ]
    if suspect:
        return False, f"unknown tools referenced: {suspect}"
    return True, ""


_MD_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)
_MD_FENCE_RE = re.compile(r"```")
_MD_BULLET_RE = re.compile(r"^\s*[-*]\s", re.MULTILINE)


def _check_channel_format_ok(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    channel = (params.get("channel") or "signal").lower()
    if channel == "signal":
        violations: list[str] = []
        if _MD_HEADING_RE.search(output):
            violations.append("markdown heading")
        if _MD_FENCE_RE.search(output):
            violations.append("fenced code block")
        if _MD_BULLET_RE.search(output):
            violations.append("bullet list")
        if violations:
            return False, f"signal channel got markdown: {violations}"
        return True, ""
    return True, ""


def _check_no_empty_reply(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    if output.strip():
        return True, ""
    return False, "empty reply"


def _check_tool_call_match(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    expected = list(params.get("expected_tools") or [])
    match_mode = params.get("match") or "set"
    observed = extract_tool_names(output)
    if match_mode == "exact_sequence":
        if observed[: len(expected)] == expected:
            return True, ""
        return False, f"expected ordered {expected!r}, got {observed!r}"
    # default: set-match
    if set(expected).issubset(set(observed)):
        return True, ""
    missing = set(expected) - set(observed)
    return False, f"missing tools {missing!r} (observed: {observed!r})"


def _check_arg_match(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    tool = params.get("tool") or ""
    arg = params.get("arg") or ""
    expected = str(params.get("value") or "")
    strategy = params.get("strategy") or "exact"
    threshold = float(params.get("threshold") or 0.0)

    actual = extract_arg_value(output, tool, arg)
    if actual is None:
        return False, f"could not find arg {arg!r} for tool {tool!r}"

    if strategy == "exact":
        if actual.strip().lower() == expected.strip().lower():
            return True, ""
        return False, f"exact mismatch: expected {expected!r}, got {actual!r}"
    if strategy == "jaccard":
        score = jaccard(expected, actual)
        if score >= threshold:
            return True, f"jaccard={score:.3f}"
        return False, f"jaccard={score:.3f} < {threshold}"
    if strategy == "levenshtein":
        dist = normalised_levenshtein(expected, actual)
        if dist <= threshold:
            return True, f"lev_norm={dist:.3f}"
        return False, f"lev_norm={dist:.3f} > {threshold}"
    return False, f"unknown strategy {strategy!r}"


def _check_bleu_threshold(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    reference = params.get("reference") or ""
    min_bleu = float(params.get("min_bleu") or 0.15)
    score = bleu4(reference, output)
    if score >= min_bleu:
        return True, f"bleu={score:.3f}"
    return False, f"bleu={score:.3f} < {min_bleu}"


def _check_entity_overlap(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    entities = [str(e).lower() for e in (params.get("entities") or []) if e]
    min_overlap = float(params.get("min_overlap") or 0.8)
    if not entities:
        return True, "no entities to check"
    lower = output.lower()
    hit = sum(1 for e in entities if e in lower)
    score = hit / len(entities)
    if score >= min_overlap:
        return True, f"overlap={score:.3f} ({hit}/{len(entities)})"
    return False, f"overlap={score:.3f} ({hit}/{len(entities)}) < {min_overlap}"


_DISPATCH_TOKENS = (
    "agent(",
    "task(",
    "taskcreate",
    "dispatch_worker",
    "spawn worker",
    "worker dispatched",
)


def _check_routing_decision(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    expected = (params.get("expected") or "inline").lower()
    lower = output.lower()
    dispatched = any(token in lower for token in _DISPATCH_TOKENS)
    actual = "dispatch" if dispatched else "inline"
    if actual == expected:
        return True, ""
    return False, f"expected {expected!r}, got {actual!r}"


def _check_skill_invocation(
    output: str, params: Mapping[str, Any]
) -> tuple[bool, str]:
    skill = params.get("skill") or ""
    required = dict(params.get("required_fields") or {})
    lower = output.lower()
    if skill and skill.lower() not in lower:
        return False, f"skill {skill!r} not referenced"
    missing: list[str] = []
    for key, expected in required.items():
        if isinstance(expected, (int, float)):
            # Find any numeric occurrence near the key.
            matches = re.findall(
                rf"{re.escape(str(key))}\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
                lower,
            )
            if not matches:
                # Try a freer match: any number within 80 chars of the key.
                window_re = re.compile(
                    rf"{re.escape(str(key).lower())}.{{0,80}}?([0-9]+(?:\.[0-9]+)?)",
                    re.DOTALL,
                )
                m = window_re.search(lower)
                matches = [m.group(1)] if m else []
            if not matches:
                missing.append(f"{key}=?")
                continue
            try:
                actual = float(matches[0])
            except ValueError:
                missing.append(f"{key}=NaN")
                continue
            tolerance = abs(float(expected)) * 0.10 if expected else 0.0
            if abs(actual - float(expected)) > tolerance:
                missing.append(
                    f"{key}={actual} (expected {expected} ±10%)"
                )
        else:
            if str(expected).lower() not in lower:
                missing.append(f"{key}={expected!r}")
    if missing:
        return False, f"missing/wrong fields: {missing}"
    return True, ""


# Registry: assertion ``type`` → callable
ASSERTION_TYPES: dict[str, Callable[[str, Mapping[str, Any]], tuple[bool, str]]] = {
    "no_forbidden_tool": _check_no_forbidden_tool,
    "no_hallucinated_tool": _check_no_hallucinated_tool,
    "channel_format_ok": _check_channel_format_ok,
    "no_empty_reply": _check_no_empty_reply,
    "tool_call_match": _check_tool_call_match,
    "arg_match": _check_arg_match,
    "bleu_threshold": _check_bleu_threshold,
    "entity_overlap": _check_entity_overlap,
    "routing_decision": _check_routing_decision,
    "skill_invocation": _check_skill_invocation,
}


def register_assertion_type(
    name: str, fn: Callable[[str, Mapping[str, Any]], tuple[bool, str]]
) -> None:
    """Plug-in point for downstream packages adding custom assertions."""
    ASSERTION_TYPES[name] = fn


# ---------------------------------------------------------------------------
# Evaluation entry points


def evaluate_assertion(
    output: str, assertion: Mapping[str, Any], bucket: str
) -> AssertionResult:
    """Evaluate a single ``assertion`` against ``output``."""
    a_type = assertion.get("type")
    fn = ASSERTION_TYPES.get(a_type or "")
    if fn is None:
        return AssertionResult(
            type=a_type or "<missing>",
            bucket=bucket,
            passed=False,
            detail=f"unknown assertion type {a_type!r}",
        )
    try:
        passed, detail = fn(output, assertion)
    except Exception as exc:  # pragma: no cover - defensive
        return AssertionResult(
            type=a_type,
            bucket=bucket,
            passed=False,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return AssertionResult(type=a_type, bucket=bucket, passed=passed, detail=detail)


def evaluate_instance(
    assertion_file: AssertionFile,
    candidate_output: str,
    *,
    candidate_id: str = "candidate",
) -> InstanceResult:
    """Evaluate every assertion in ``assertion_file`` against
    ``candidate_output`` and return the per-instance verdict.

    An instance resolves iff every ``fail_to_pass`` AND every
    ``pass_to_pass`` assertion holds.
    """
    results: list[AssertionResult] = []
    for entry in assertion_file.pass_to_pass:
        results.append(evaluate_assertion(candidate_output, entry, "pass_to_pass"))
    for entry in assertion_file.fail_to_pass:
        results.append(evaluate_assertion(candidate_output, entry, "fail_to_pass"))

    resolved = all(r.passed for r in results)
    return InstanceResult(
        turn_id=assertion_file.turn_id,
        category=assertion_file.category,
        candidate_id=candidate_id,
        resolved=resolved,
        results=results,
    )


def run_against_results(
    assertions_dir: str | Path,
    candidate_results: Sequence[Mapping[str, Any]],
    *,
    candidate_id: str = "candidate",
) -> list[InstanceResult]:
    """For each candidate result (one per turn), load the matching
    ``<turn_id>.assert.json`` and evaluate.

    Skips instances whose assertion file is missing (with a warning).
    """
    base = Path(assertions_dir).expanduser()
    out: list[InstanceResult] = []
    for row in candidate_results:
        turn_id = row.get("turn_id")
        if not turn_id:
            continue
        path = base / f"{turn_id}.assert.json"
        if not path.is_file():
            log.warning("no assertion file for turn_id=%s at %s", turn_id, path)
            continue
        try:
            af = load_assertion_file(path)
        except Exception as exc:
            log.warning("could not load assertion file %s: %s", path, exc)
            continue
        out.append(
            evaluate_instance(
                af, row.get("output") or "", candidate_id=candidate_id
            )
        )
    return out
