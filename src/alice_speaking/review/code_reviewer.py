"""Code-quality reviewer — Sonnet sub-agent for ``(reviewing, art:code)``.

Sibling to :data:`alice_thinking.design_pipeline.REVIEWER_SYSTEM_PROMPT`.
The design reviewer judges design *drafts* against a commission spec;
this reviewer judges *code PRs* against the issue requirements they
claim to close.

Same JSON contract as the design reviewer — verdict gate
(``approved`` | ``needs_revision``), severity taxonomy
(``critical`` | ``major`` | ``minor``), no markdown fences — but a
different category whitelist tuned for implementation quality:

- ``test_adequacy`` — are tests present and exercising the new behavior?
- ``security`` — does the change introduce or fail to mitigate a
  security issue (auth, injection, secrets handling, etc.)?
- ``performance`` — does the change introduce avoidable hot-path costs
  or regressions?
- ``error_handling`` — are failure modes handled at the boundary they
  belong to (vs. swallowed / propagated badly)?
- ``naming_and_clarity`` — do identifiers and structure communicate
  intent? Smell-only — not a license to bikeshed.
- ``requirements_coverage`` — does the diff actually implement what
  the linked issue asked for?

The dispatcher integration that consumes the verdict
(``sm:reviewing → sm:done`` on approval, ``sm:reviewing → sm:building``
on needs_revision) is wired separately; see issue #107's integration
section and the spawn-map entry in ``alice_sm.dispatcher``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional


__all__ = [
    "CODE_REVIEW_CATEGORIES",
    "CODE_REVIEWER_SYSTEM_PROMPT",
    "CodeReviewResult",
]


CODE_REVIEW_CATEGORIES: frozenset[str] = frozenset(
    {
        "test_adequacy",
        "security",
        "performance",
        "error_handling",
        "naming_and_clarity",
        "requirements_coverage",
    }
)


CODE_REVIEWER_SYSTEM_PROMPT = """You are a structural reviewer for code pull requests.

The user message contains the linked issue (what was asked for) and the
candidate PR diff (what was implemented). Review whether the diff
actually implements the issue and meets a baseline of implementation
quality.

Verdict rules:
- ``approved`` — no critical or major issues; minor nits acceptable.
- ``needs_revision`` — at least one critical or major issue.

Categories you may flag (use these names verbatim; do not invent new
ones):

- test_adequacy — are tests present that exercise the new/changed
  behavior? Bug fixes need a regression test; new features need
  coverage of the golden path and meaningful edge cases.
- security — does the change introduce or fail to mitigate a security
  issue? Examples: auth bypass, injection (SQL/command/template),
  secrets in code or logs, unsafe deserialization, missing validation
  at a trust boundary.
- performance — does the change introduce avoidable hot-path costs?
  Examples: N+1 queries, unbounded iteration, blocking I/O in an
  async path, accidental quadratic complexity on user-controlled
  input.
- error_handling — are failure modes handled at the right boundary?
  Flag swallowed exceptions, bare ``except``, errors raised across
  unrelated abstraction layers, retries on non-idempotent operations.
- naming_and_clarity — do identifiers and structure communicate
  intent? Flag genuinely misleading names or buried structure. This
  is the smell category, not a license to bikeshed — prefer ``minor``
  severity unless the confusion is load-bearing.
- requirements_coverage — does the diff actually implement what the
  linked issue asked for? Flag missing acceptance criteria, partial
  implementations, or scope drift (work that wasn't requested).

Severity rules:
- ``critical`` — security issue, data-loss risk, behavior contradicts
  the issue's acceptance criteria, or tests are missing for a
  bug-fix/regression.
- ``major`` — performance regression on a hot path, error handling
  that masks failures, requirements partially unaddressed.
- ``minor`` — naming nits, non-load-bearing clarity issues, doc gaps.

Return STRICT JSON matching this schema. No prose, no markdown fences.

{
  "verdict": "approved" | "needs_revision",
  "confidence": <float 0..1>,
  "summary": "<one-line verdict summary>",
  "feedback": [
    {
      "category": "<one of the categories above>",
      "severity": "critical" | "major" | "minor",
      "description": "<what's wrong>",
      "location": "<file path or 'global'>"
    }
  ],
  "patterns": ["<recurring patterns, if any>"]
}
"""


@dataclass(frozen=True)
class CodeReviewResult:
    """Parsed Sonnet code-review output.

    Mirrors :class:`alice_thinking.design_pipeline.ReviewResult` — same
    JSON shape, same verdict gate — but :meth:`parse_json` filters
    feedback entries whose ``category`` is outside
    :data:`CODE_REVIEW_CATEGORIES`. The reviewer's system prompt is the
    source of truth for which categories are legal; the parser
    enforces that contract at the boundary so callers downstream don't
    have to.
    """

    verdict: str  # "approved" | "needs_revision"
    confidence: float
    summary: str
    feedback: list[dict[str, str]] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    raw: Optional[str] = None

    @classmethod
    def parse_json(cls, text: str) -> "CodeReviewResult":
        r"""Parse a code-review verdict from ``text``.

        Tolerates code fences (``\`\`\`json ... \`\`\``) and surrounding
        whitespace; rejects payloads that don't decode, aren't a JSON
        object, or carry an unrecognized ``verdict``. Feedback entries
        with categories outside :data:`CODE_REVIEW_CATEGORIES` are
        dropped silently — the system prompt instructs the reviewer
        not to invent categories, but we don't trust the model to obey
        every time.
        """
        body = _strip_json_fences(text)
        try:
            blob = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"code reviewer returned invalid JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise ValueError("code reviewer JSON must be an object")
        verdict = str(blob.get("verdict", "")).strip()
        if verdict not in ("approved", "needs_revision"):
            raise ValueError(
                f"code reviewer returned unexpected verdict: {verdict!r}"
            )

        raw_feedback = blob.get("feedback") or []
        if not isinstance(raw_feedback, list):
            raw_feedback = []
        normalized: list[dict[str, str]] = []
        for fb in raw_feedback:
            if not isinstance(fb, dict):
                continue
            category = str(fb.get("category", ""))
            if category not in CODE_REVIEW_CATEGORIES:
                continue
            normalized.append({k: str(v) for k, v in fb.items()})

        patterns = blob.get("patterns") or []
        if not isinstance(patterns, list):
            patterns = []

        try:
            confidence = float(blob.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        return cls(
            verdict=verdict,
            confidence=confidence,
            summary=str(blob.get("summary", "")),
            feedback=normalized,
            patterns=[str(p) for p in patterns],
            raw=text,
        )


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    body = text.strip()
    m = _FENCE_RE.match(body)
    if m:
        return m.group(1).strip()
    return body
