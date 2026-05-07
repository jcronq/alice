"""Design Commission Pipeline — runtime-driven design-doc review loop.

Folds into the phase-routing system as :attr:`Phase.DESIGN_COMMISSION`.
Triggered when a note dropped in ``inner/notes/`` carries
``task_type: design-commission`` (or matches the filename / folder
fallbacks; see :func:`alice_thinking.phase.detect_commission_notes`).

Loop:

1. :class:`DesignPipelineRunner` reads the commission spec.
2. Qwen drafts a design (first iteration uses the spec verbatim).
3. :class:`SubAgentRunner` invokes Sonnet via ``claude-agent-sdk``
   to review the draft + spec; reviewer returns structured JSON.
4. If ``verdict: approved`` → stop, commit draft, surface
   ``design-commission-result``.
5. Else revise (Qwen sees spec + draft + structured feedback) and
   loop, max 3 iterations.
6. If cap is hit without approval → DO NOT commit; surface
   ``design-commission-cap-hit`` for human review with the
   unresolved feedback attached.

Auth — SubAgentRunner reuses Speaking's existing credentials at
``~/.claude/.credentials.json`` (already mounted into the worker
container). The narrative in
``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``
nominates ``claude-agent-sdk``; we pick that for parity with
Speaking's auth path. No new secret, no new compose volume.

State is in-memory — nothing persists until the final commit (or
cap-hit surface). Per the design doc, wake duration of ~30 minutes
is acceptable for commission tasks.

Speaking is not in the loop. It only consumes the final surface.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional


__all__ = [
    "ReviewResult",
    "PipelineResult",
    "SubAgentRunner",
    "DesignPipelineRunner",
    "REVIEWER_SYSTEM_PROMPT",
    "DEFAULT_REVIEWER_MODEL",
]


DEFAULT_REVIEWER_MODEL = "claude-sonnet-4-6"


REVIEWER_SYSTEM_PROMPT = """You are a structural reviewer for design documents.

The user message contains the original commission spec and a candidate
draft response to that spec. Review whether the draft solves the stated
problem and addresses the requirements in the spec.

Verdict rules:
- ``approved`` — no critical or major structural issues; minor nits
  acceptable.
- ``needs_revision`` — at least one critical or major issue.

Categories you may flag:

- problem_solving — does the design actually solve the stated problem?
- layer_boundaries — are module/layer responsibilities clean?
- requirements_coverage — does it cover every requirement in the spec?
- migration_plan — is there a workable rollout?
- unaddressed_cases — known edge cases the design ignores.
- patterns — recurring structural smells worth naming.

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
      "location": "<section name or 'global'>"
    }
  ],
  "patterns": ["<recurring patterns, if any>"]
}
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewResult:
    """Parsed Sonnet review output."""

    verdict: str  # "approved" | "needs_revision"
    confidence: float
    summary: str
    feedback: list[dict[str, str]] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    raw: Optional[str] = None

    @classmethod
    def parse_json(cls, text: str) -> "ReviewResult":
        """Parse JSON from ``text``. Tolerates fenced code blocks +
        leading/trailing whitespace; raises ``ValueError`` if the
        payload doesn't decode."""
        body = _strip_json_fences(text)
        try:
            blob = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"reviewer returned invalid JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise ValueError("reviewer JSON must be an object")
        verdict = str(blob.get("verdict", "")).strip()
        if verdict not in ("approved", "needs_revision"):
            raise ValueError(f"reviewer returned unexpected verdict: {verdict!r}")
        feedback = blob.get("feedback") or []
        if not isinstance(feedback, list):
            feedback = []
        # Normalize each feedback entry to a flat dict-of-strings.
        normalized: list[dict[str, str]] = []
        for fb in feedback:
            if not isinstance(fb, dict):
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


@dataclass
class PipelineResult:
    """Final output of one commission run."""

    iteration_count: int
    verdict: str  # "approved" | "cap_hit"
    final_round: int
    draft: str
    summary: str
    last_feedback: list[dict[str, str]] = field(default_factory=list)
    output_path: Optional[pathlib.Path] = None
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Sub-agent — Sonnet reviewer
# ---------------------------------------------------------------------------


class SubAgentRunner:
    """Spawn a Sonnet subagent to review a draft against its spec.

    Uses ``claude-agent-sdk`` (the same SDK Speaking uses) so the
    Anthropic credentials at ``~/.claude/.credentials.json`` flow
    through the existing OAuth path. No new env var, no new mount.

    The ``review_text`` method is the seam tests stub out — production
    runs hit the real SDK; tests inject a fake.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_REVIEWER_MODEL,
        system_prompt: str = REVIEWER_SYSTEM_PROMPT,
        max_seconds: int = 600,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.max_seconds = max_seconds

    def review(self, spec_path: pathlib.Path, draft: str) -> ReviewResult:
        """Synchronous review entry point.

        Builds the user message from the spec at ``spec_path`` and the
        in-memory draft, then drives the SDK via :meth:`review_text`.
        """
        spec = spec_path.read_text(encoding="utf-8")
        prompt = (
            "Here is the original commission spec:\n\n"
            f"{spec}\n\n---\n\nAnd here is the draft to review:\n\n"
            f"{draft}\n\nReview whether this draft solves the stated "
            "problem and addresses the requirements in the spec."
        )
        raw = self.review_text(prompt)
        return ReviewResult.parse_json(raw)

    def review_text(self, prompt: str) -> str:
        """Drive ``claude-agent-sdk`` with no tool access; return the
        concatenated assistant text. Override this in tests.
        """
        return asyncio.run(self._review_via_sdk(prompt))

    async def _review_via_sdk(self, prompt: str) -> str:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )

        options = ClaudeAgentOptions(
            model=self.model,
            allowed_tools=[],
            system_prompt={"type": "preset", "preset": "claude_code", "append": self.system_prompt},
        )
        parts: list[str] = []
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
        return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Reviser — Qwen / local thinking model handles the rewrites.
# ---------------------------------------------------------------------------


class _NullReviser:
    """Default reviser used when no real reviser is wired.

    Returns the draft unchanged with a comment trailer noting the
    feedback. Production runs should pass a custom callable that
    invokes Qwen via :class:`PhaseRunner` — the Speaking-side
    integration is deferred per the design doc.
    """

    def __call__(
        self,
        *,
        spec: str,
        draft: str,
        feedback: list[dict[str, str]],
    ) -> str:
        feedback_text = "\n".join(
            f"- [{f.get('severity', '?')}] {f.get('category', '?')}: "
            f"{f.get('description', '')} (at {f.get('location', 'global')})"
            for f in feedback
        )
        trailer = (
            "\n\n---\n\n"
            "_Reviser stub: real Qwen dispatch pending. Outstanding "
            f"feedback:_\n\n{feedback_text}\n"
        )
        return draft + trailer


def build_revision_prompt(
    *, spec: str, draft: str, feedback: list[dict[str, str]]
) -> str:
    """Compose the user message for a Qwen revision turn.

    Three inputs in one message: the original commission spec, the
    current draft, and Sonnet's structured feedback. The prompt
    instructs the model to revise every critical and major issue.
    """
    feedback_text = "\n\n".join(
        f"- [{f.get('severity', '?')}] {f.get('category', '?')}: "
        f"{f.get('description', '')} (at {f.get('location', 'global')})"
        for f in feedback
    )
    return (
        "Here is the commission spec I was asked to design:\n\n"
        f"{spec}\n\n"
        "---\n\n"
        "Here is my current draft:\n\n"
        f"{draft}\n\n"
        "---\n\n"
        "Sonnet's review feedback (these are the issues that must be "
        "fixed before approval):\n\n"
        f"{feedback_text}\n\n"
        "Revise the draft to address every critical and major issue "
        "listed above. Preserve all sections that Sonnet did not flag. "
        "Return the complete revised draft."
    )


# ---------------------------------------------------------------------------
# Pipeline runner — owns the loop control.
# ---------------------------------------------------------------------------


class DesignPipelineRunner:
    """Drive the commission loop: draft → review → revision → ..."""

    MAX_ITERATIONS = 3

    def __init__(
        self,
        *,
        reviewer: Optional[SubAgentRunner] = None,
        reviser: Optional[Any] = None,
        max_iterations: Optional[int] = None,
    ) -> None:
        self._reviewer = reviewer or SubAgentRunner()
        self._reviser = reviser or _NullReviser()
        self._max_iterations = max_iterations or self.MAX_ITERATIONS

    def run(self, commission_note: pathlib.Path) -> PipelineResult:
        """Drive the loop. Returns a :class:`PipelineResult`.

        ``commission_note`` is the spec on disk — the draft starts as
        the note's body and is revised in-memory across iterations.
        """
        spec = commission_note.read_text(encoding="utf-8")
        draft = spec
        last_feedback: list[dict[str, str]] = []
        last_summary = ""
        started = time.time()

        for iteration in range(1, self._max_iterations + 1):
            if iteration > 1:
                draft = self._reviser(
                    spec=spec, draft=draft, feedback=last_feedback
                )
            review = self._reviewer.review(commission_note, draft)
            last_feedback = list(review.feedback)
            last_summary = review.summary

            if review.verdict == "approved":
                return PipelineResult(
                    iteration_count=iteration,
                    verdict="approved",
                    final_round=iteration,
                    draft=draft,
                    summary=review.summary,
                    last_feedback=last_feedback,
                    duration_seconds=time.time() - started,
                )

        # Cap hit — surface the unresolved feedback. Do NOT commit.
        return PipelineResult(
            iteration_count=self._max_iterations,
            verdict="cap_hit",
            final_round=self._max_iterations,
            draft=draft,
            summary=last_summary
            or f"Cap hit after {self._max_iterations} iterations",
            last_feedback=last_feedback,
            duration_seconds=time.time() - started,
        )


# ---------------------------------------------------------------------------
# Surface emission + draft commit helpers
# ---------------------------------------------------------------------------


def commit_approved_draft(
    mind: pathlib.Path,
    *,
    draft: str,
    slug_hint: str,
    now: Optional[_dt.datetime] = None,
) -> pathlib.Path:
    """Write the approved draft to ``cortex-memory/research/<date>-<slug>.md``.

    The slug is sanitized (lowercase, alphanumeric + hyphens). The
    function does NOT add frontmatter — the draft is expected to
    carry its own. Returns the path written.
    """
    now = now or _dt.datetime.now()
    safe = re.sub(r"[^a-z0-9-]+", "-", slug_hint.lower()).strip("-") or "draft"
    research = mind / "cortex-memory" / "research"
    research.mkdir(parents=True, exist_ok=True)
    path = research / f"{now.date().isoformat()}-{safe}.md"
    # If a same-name file exists, append a disambiguating suffix.
    if path.exists():
        path = research / f"{now.date().isoformat()}-{safe}-{int(now.timestamp())}.md"
    path.write_text(draft, encoding="utf-8")
    return path


def write_surface(
    mind: pathlib.Path,
    *,
    surface_type: str,
    body: str,
    now: Optional[_dt.datetime] = None,
    extra_frontmatter: Optional[dict[str, Any]] = None,
) -> pathlib.Path:
    """Write a surface note to ``inner/surface/<utcstamp>-<type>.md``."""
    now = now or _dt.datetime.now()
    surface_dir = mind / "inner" / "surface"
    surface_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    safe_type = re.sub(r"[^a-z0-9-]+", "-", surface_type.lower()).strip("-")
    path = surface_dir / f"{stamp}-{safe_type}.md"

    fm: dict[str, Any] = {
        "priority": "insight",
        "context": surface_type,
        "reply_expected": "false",
        "type": surface_type,
    }
    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            fm[k] = v

    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v)}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Telemetry payload helpers
# ---------------------------------------------------------------------------


def telemetry_payload(result: PipelineResult, *, phase_value: str) -> dict[str, Any]:
    """Build the keyword fields for the ``design_commission`` event."""
    return {
        "task_type": "design-commission",
        "phase": phase_value,
        "iteration_count": result.iteration_count,
        "verdict": result.verdict,
        "final_round": result.final_round,
        "total_wake_seconds": float(result.duration_seconds),
    }
