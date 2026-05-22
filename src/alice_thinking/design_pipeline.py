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

As of Phase 3 of #194 the dispatch path no longer constructs
:class:`ClaudeAgentOptions` inline — it looks up the registered
``"reviewer"`` :class:`AgentSpec` from
:data:`core.agent_library.default_registry` and dispatches via
:func:`core.agent_library.run_agent`. The Sonnet model + structured-
JSON behavior is preserved byte-identically via a per-call
:func:`dataclasses.replace` that swaps the registry's code-reviewer
system prompt for this module's design-reviewer prompt.

State is in-memory — nothing persists until the final commit (or
cap-hit surface). Per the design doc, wake duration of ~30 minutes
is acceptable for commission tasks.

Speaking is not in the loop. It only consumes the final surface.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional


if TYPE_CHECKING:
    from core.config.model import BackendSpec
    from core.kernel import KernelSpec
    from alice_thinking.runtime import PhaseRunner


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
        return asyncio.run(self._review_via_agent_library(prompt))

    async def _review_via_agent_library(self, prompt: str) -> str:
        """Dispatch the review turn through :func:`run_agent` against the
        registered ``"reviewer"`` :class:`AgentSpec`.

        Phase 3 of #194 replaced the inline :class:`ClaudeAgentOptions`
        construction with a registry lookup. The registered spec carries
        the Sonnet model + read-only tool policy + verdict-gate
        behavioral rules; we override ``append_system_prompt`` with this
        runner's :attr:`system_prompt` (the design-doc reviewer prompt,
        which differs from the code-reviewer canonical prompt the
        registered spec references). The override is the per-call
        pattern documented in :func:`core.agent_library.runner.run_agent`.
        """
        from dataclasses import replace

        from core.agent_library import default_registry, run_agent
        from core.events import CapturingEmitter

        spec = default_registry.get("reviewer")
        # Per-call override: swap in this runner's model + system prompt
        # without mutating the registered spec. The design-pipeline
        # reviewer reviews design *drafts* against a commission spec —
        # different prompt body than the code-reviewer the registry
        # entry points to. Allowed_tools stays empty via the
        # registered read_only policy (Read/Glob/Grep) clamped by the
        # explicit override below — we want zero tool access for the
        # structured-JSON verdict turn.
        kernel = replace(
            spec.kernel_spec,
            model=self.model,
            max_seconds=self.max_seconds,
            allowed_tools=[],
            append_system_prompt=self.system_prompt,
        )
        # Drop the tool_policy too — the override above already pinned
        # allowed_tools to []; the policy's allowlist would reintroduce
        # the read-only tools at effective_tools() time.
        agent = replace(spec, kernel_spec=kernel, tool_policy=None)

        result = await run_agent(
            agent,
            prompt=prompt,
            emitter=CapturingEmitter(),
            correlation_id="design-pipeline-reviewer",
        )
        return (result.text or "").strip()


# ---------------------------------------------------------------------------
# Reviser — Qwen / local thinking model handles the rewrites.
# ---------------------------------------------------------------------------


class _NullReviser:
    """Documented test helper — appends feedback as a markdown trailer.

    Kept after the v1 wire-up of :class:`_QwenReviser` because some
    tests still want a reviser that produces deterministic, kernel-free
    output. Production no longer uses this — the default reviser is
    :class:`_QwenReviser`.
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


class _QwenReviser:
    """Real Qwen reviser — dispatches revision turns through PhaseRunner.

    See ``cortex-memory/research/2026-05-07-qwen-reviser-wireup-design.md``.
    Replaces :class:`_NullReviser` as the default reviser on
    :class:`DesignPipelineRunner`. Each call dispatches one revision
    turn (or one+retry on short output) through the standard
    :class:`PhaseRunner` path with :attr:`Phase.REVISE`, executes the
    resulting :class:`KernelSpec` via :func:`make_kernel`, validates the
    output length, and emits structured telemetry for each attempt.

    Failure modes (per D3 of the design):

    - **Timeout** (kernel call exceeds :attr:`MAX_REVISION_SECONDS`):
      emit ``design_commission_revision`` with ``verdict=timeout``,
      return the input draft unchanged. No retry — cap-hit logic in
      the runner takes over after the iteration cap is reached.
    - **Malformed** (output length < :attr:`MIN_VALID_OUTPUT_RATIO` *
      input draft length, or non-string output): emit ``verdict=malformed``,
      log the raw output to ``/tmp/qwen-reviser-malformed-<ts>.txt``
      for forensics, return draft unchanged. No retry — sub-50% length
      is non-retryable garbage.
    - **Short output** (output length < :attr:`MIN_OUTPUT_RATIO` *
      input draft length but >= :attr:`MIN_VALID_OUTPUT_RATIO`):
      emit ``verdict=short_output``, retry once with the same inputs.
      If retry also short, return draft unchanged. If retry succeeds,
      continue normally.

    Test seams:

    - ``phase_runner=`` constructor parameter — tests inject a stub /
      ``Mock`` to assert :attr:`Phase.REVISE` was the dispatched phase.
    - ``_execute_kernel_call(prompt, spec)`` method — synchronous
      seam tests patch to simulate kernel responses (success, short,
      malformed, :class:`asyncio.TimeoutError`). Returning a string
      from this method short-circuits the real kernel dispatch.
    """

    MAX_REVISION_SECONDS = 300
    MIN_OUTPUT_RATIO = 0.75
    MIN_VALID_OUTPUT_RATIO = 0.50
    MAX_RETRIES = 1

    def __init__(
        self,
        backend_spec: Optional["BackendSpec"] = None,
        phase_runner: Optional["PhaseRunner"] = None,
        wake_context: Optional[Any] = None,
        emitter: Optional[Any] = None,
    ) -> None:
        # Single owner of backend loading (D2). The runner has no
        # ``_get_backend_spec`` — the kernel is the reviser's
        # dependency.
        self._backend_spec = backend_spec
        self._phase_runner = phase_runner
        self._wake_context = wake_context
        self._emitter = emitter
        # In-process telemetry buffer — tests inspect this directly;
        # the parent runner pulls events into its result aggregate.
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Public entry — composes the revision prompt + drives the loop.

    def __call__(
        self,
        *,
        spec: str,
        draft: str,
        feedback: list[dict[str, str]],
    ) -> str:
        revision_prompt = build_revision_prompt(
            spec=spec, draft=draft, feedback=feedback
        )
        critical = sum(1 for f in feedback if f.get("severity") == "critical")
        major = sum(1 for f in feedback if f.get("severity") == "major")
        return self._run_revision(
            revision_prompt=revision_prompt,
            draft=draft,
            feedback_count=len(feedback),
            critical_count=critical,
            major_count=major,
        )

    # ------------------------------------------------------------------ #
    # Loop control: dispatch + validation + retry.

    def _run_revision(
        self,
        *,
        revision_prompt: str,
        draft: str,
        feedback_count: int,
        critical_count: int,
        major_count: int,
    ) -> str:
        """Drive one revision call (with at most one retry on short output).

        Returns the revised draft string on ``ok`` verdict, or the
        ``draft`` argument unchanged on any failure path. Always emits
        one ``design_commission_revision`` telemetry event per attempt.
        """
        runner = self._get_phase_runner()
        ctx = self._get_wake_context()

        # Compose prompt + spec via the standard PhaseRunner path so
        # the prelude + identity are preserved (D1/D5 unification).
        from .phase import Phase  # local import keeps module load cheap

        prompt_text, kernel_spec = runner.run(
            Phase.REVISE, ctx, injected_content=revision_prompt
        )

        attempts = self.MAX_RETRIES + 1
        last_text = draft
        for attempt in range(1, attempts + 1):
            started = time.time()
            try:
                output = self._execute_kernel_call(prompt_text, kernel_spec)
            except asyncio.TimeoutError:
                self._emit_event(
                    revision_seconds=time.time() - started,
                    draft_input_chars=len(draft),
                    draft_output_chars=0,
                    output_length_ratio=0.0,
                    verdict="timeout",
                    feedback_count=feedback_count,
                    critical_count=critical_count,
                    major_count=major_count,
                    attempt=attempt,
                )
                return draft

            elapsed = time.time() - started

            # Malformed: not a string, or below the 50% non-retryable
            # threshold. Log raw output to /tmp for forensics.
            if not isinstance(output, str):
                self._log_malformed(output)
                self._emit_event(
                    revision_seconds=elapsed,
                    draft_input_chars=len(draft),
                    draft_output_chars=0,
                    output_length_ratio=0.0,
                    verdict="malformed",
                    feedback_count=feedback_count,
                    critical_count=critical_count,
                    major_count=major_count,
                    attempt=attempt,
                )
                return draft

            output = output.strip()
            ratio = (len(output) / len(draft)) if draft else 1.0

            if ratio < self.MIN_VALID_OUTPUT_RATIO:
                self._log_malformed(output)
                self._emit_event(
                    revision_seconds=elapsed,
                    draft_input_chars=len(draft),
                    draft_output_chars=len(output),
                    output_length_ratio=ratio,
                    verdict="malformed",
                    feedback_count=feedback_count,
                    critical_count=critical_count,
                    major_count=major_count,
                    attempt=attempt,
                )
                return draft

            if ratio < self.MIN_OUTPUT_RATIO:
                # Short output — retry-worthy. Emit the short_output
                # event for THIS attempt, then either retry or fall
                # back to the input draft.
                self._emit_event(
                    revision_seconds=elapsed,
                    draft_input_chars=len(draft),
                    draft_output_chars=len(output),
                    output_length_ratio=ratio,
                    verdict="short_output",
                    feedback_count=feedback_count,
                    critical_count=critical_count,
                    major_count=major_count,
                    attempt=attempt,
                )
                last_text = output
                if attempt < attempts:
                    continue
                # Out of retries — return input draft unchanged.
                return draft

            # Output >= 75% of draft → ok.
            self._emit_event(
                revision_seconds=elapsed,
                draft_input_chars=len(draft),
                draft_output_chars=len(output),
                output_length_ratio=ratio,
                verdict="ok",
                feedback_count=feedback_count,
                critical_count=critical_count,
                major_count=major_count,
                attempt=attempt,
            )
            return output

        # Defensive: should be unreachable — the loop above either
        # returns or `continue`s through every iteration.
        return last_text  # pragma: no cover

    # ------------------------------------------------------------------ #
    # Kernel dispatch — test seam.

    def _execute_kernel_call(
        self, prompt_text: str, kernel_spec: "KernelSpec"
    ) -> str:
        """Synchronous kernel-execution seam.

        Wraps the async :meth:`Kernel.run` call with
        :func:`asyncio.wait_for` (per-revision budget enforced via
        :attr:`MAX_REVISION_SECONDS`), then runs it via
        :func:`asyncio.run`. Tests patch this method directly — the
        production path runs the real kernel.

        Raises :class:`asyncio.TimeoutError` when the per-revision
        budget elapses; returns the kernel's text output on success.
        Other kernel errors propagate; the caller treats them as
        ``malformed`` and records the raw payload.
        """
        from core.kernel import make_kernel
        from core.events import CapturingEmitter

        backend = self._get_backend_spec()
        # CapturingEmitter is a quiet sink — kernel-level events emitted
        # during a revision turn are captured but not forwarded. The
        # design-pipeline emitter receives the aggregate revision event
        # via ``self._emit_event``.
        emitter = CapturingEmitter()
        kernel = make_kernel(backend, emitter, correlation_id="qwen-reviser")

        async def _run() -> str:
            result = await asyncio.wait_for(
                kernel.run(prompt_text, kernel_spec),
                timeout=self.MAX_REVISION_SECONDS,
            )
            return result.text or ""

        return asyncio.run(_run())

    # ------------------------------------------------------------------ #
    # Backend / runner / wake-context loaders.

    def _get_backend_spec(self) -> "BackendSpec":
        if self._backend_spec is None:
            self._backend_spec = self._load_backend()
        return self._backend_spec

    def _load_backend(self) -> "BackendSpec":
        """Load the thinking backend from ``mind/config/model.yml``.

        Single owner — :class:`DesignPipelineRunner` has no parallel
        backend-loading method (D2 of the design).
        """
        from core.config.model import load as load_model_config

        mind = pathlib.Path(
            os.environ.get("ALICE_MIND")
            or pathlib.Path.home() / "alice-mind"
        )
        cfg = load_model_config(mind)
        return cfg.thinking

    def _get_phase_runner(self) -> "PhaseRunner":
        if self._phase_runner is None:
            from alice_thinking.runtime import PhaseRunner

            self._phase_runner = PhaseRunner()
        return self._phase_runner

    def _get_wake_context(self) -> Any:
        """Return a :class:`WakeContext` for the revision dispatch.

        Built once per reviser instance. The context plumbs the
        backend's model name through to the :class:`KernelSpec`
        produced by :class:`PhaseRunner`. Tests inject a custom
        context via the ``wake_context`` constructor kwarg; the
        default produces a minimal context tied to the resolved
        backend spec.
        """
        if self._wake_context is not None:
            return self._wake_context

        from core.config.personae import placeholder
        from alice_thinking.modes.base import WakeContext

        backend = self._get_backend_spec()
        mind = pathlib.Path(
            os.environ.get("ALICE_MIND")
            or pathlib.Path.home() / "alice-mind"
        )
        cwd = pathlib.Path(
            os.environ.get("ALICE_THINKING_CWD") or pathlib.Path.cwd()
        )
        self._wake_context = WakeContext(
            mind_dir=mind,
            cwd=cwd,
            now=_dt.datetime.now(),
            personae=placeholder(),
            model=backend.model or "",
            max_seconds=self.MAX_REVISION_SECONDS,
            tools=[],  # Phase.REVISE = no tools allowed
            system_prompt="",
            quick=False,
            inline_prompt=None,
            bootstrap_path=None,
            directive_path=None,
        )
        return self._wake_context

    # ------------------------------------------------------------------ #
    # Telemetry.

    def _emit_event(self, **fields: Any) -> None:
        event = {
            "type": "design_commission_revision",
            **fields,
        }
        self.events.append(event)
        if self._emitter is not None:
            try:
                self._emitter.emit("design_commission_revision", **fields)
            except Exception:  # noqa: BLE001 - telemetry must never raise
                pass

    @staticmethod
    def _log_malformed(payload: Any) -> Optional[pathlib.Path]:
        """Persist a malformed kernel payload to /tmp for post-mortem.

        Best-effort — failure to write the artifact must not propagate
        and break the revision flow. Returns the path written or
        ``None`` if the write failed.
        """
        try:
            ts = int(time.time() * 1000)
            path = pathlib.Path("/tmp") / f"qwen-reviser-malformed-{ts}.txt"
            body = (
                payload
                if isinstance(payload, str)
                else repr(payload)
            )
            path.write_text(body, encoding="utf-8", errors="replace")
            return path
        except OSError:
            return None


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
        self._reviser = reviser or _QwenReviser()
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
    evidence: Optional[Any] = None,
) -> pathlib.Path:
    """Write a surface note to ``inner/surface/<utcstamp>-<type>.md``.

    When ``extra_frontmatter`` declares ``priority: flash``, the surface
    guard (:func:`alice_thinking.surface_guard.should_fire`) is consulted
    against ``evidence`` before the write. On a failing check, priority is
    downgraded to ``insight`` and ``guard_reason`` is stamped into the
    frontmatter so Thinking can see why on the next wake. Insight-tier
    surfaces pass through unguarded — the guard is flash-only.
    """
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

    # Surface guard — check claim verifiability before firing a flash surface.
    if fm.get("priority") == "flash":
        from alice_thinking.surface_guard import should_fire

        can_fire, reason = should_fire(
            target=surface_type,
            evidence=evidence if evidence is not None else {},
            priority="flash",
        )
        if not can_fire:
            fm["priority"] = "insight"
            fm["guard_reason"] = reason

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
