"""Tests for ``alice_thinking.design_pipeline``.

Pin: loop control + verdict-driven side effects (commit on
approved, no commit on cap-hit), structured-output parsing,
revision-prompt composition, surface emission shape, the Qwen
reviser's failure-mode handling (timeout/malformed/short-output)
and its Phase.REVISE PhaseRunner dispatch.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from alice_thinking.design_pipeline import (
    DesignPipelineRunner,
    PipelineResult,
    ReviewResult,
    SubAgentRunner,
    _NullReviser,
    _QwenReviser,
    build_revision_prompt,
    commit_approved_draft,
    telemetry_payload,
    write_surface,
)
from alice_thinking.phase import Phase


class _ScriptedReviewer(SubAgentRunner):
    """Stub reviewer that returns canned results from a queue."""

    def __init__(self, *responses: ReviewResult) -> None:
        super().__init__()
        self._queue: list[ReviewResult] = list(responses)
        self.calls: list[tuple[pathlib.Path, str]] = []

    def review(self, spec_path: pathlib.Path, draft: str) -> ReviewResult:
        self.calls.append((spec_path, draft))
        if not self._queue:
            raise RuntimeError("scripted reviewer ran out of responses")
        return self._queue.pop(0)


def _approved(summary: str = "ok") -> ReviewResult:
    return ReviewResult(
        verdict="approved", confidence=0.9, summary=summary, feedback=[]
    )


def _needs_revision(summary: str = "fix it", n: int = 1) -> ReviewResult:
    return ReviewResult(
        verdict="needs_revision",
        confidence=0.6,
        summary=summary,
        feedback=[
            {
                "category": "problem_solving",
                "severity": "critical",
                "description": f"issue {i}",
                "location": "section 1",
            }
            for i in range(n)
        ],
    )


# ---------------------------------------------------------------------------
# ReviewResult parsing
# ---------------------------------------------------------------------------


def test_review_result_parses_clean_json() -> None:
    payload = {
        "verdict": "approved",
        "confidence": 0.9,
        "summary": "looks good",
        "feedback": [],
        "patterns": [],
    }
    res = ReviewResult.parse_json(json.dumps(payload))
    assert res.verdict == "approved"
    assert res.summary == "looks good"
    assert res.feedback == []


def test_review_result_strips_code_fences() -> None:
    body = '```json\n{"verdict": "approved", "confidence": 0.5, "summary": "ok"}\n```'
    res = ReviewResult.parse_json(body)
    assert res.verdict == "approved"


def test_review_result_rejects_unknown_verdict() -> None:
    with pytest.raises(ValueError):
        ReviewResult.parse_json('{"verdict": "maybe", "summary": "x"}')


def test_review_result_normalizes_feedback_dicts() -> None:
    payload = {
        "verdict": "needs_revision",
        "summary": "x",
        "feedback": [
            {"category": "problem_solving", "severity": "major"},
            "not-a-dict",
        ],
    }
    res = ReviewResult.parse_json(json.dumps(payload))
    # Non-dict feedback entries are dropped; remaining entries are
    # flattened to dict-of-strings.
    assert len(res.feedback) == 1
    assert res.feedback[0]["category"] == "problem_solving"


# ---------------------------------------------------------------------------
# DesignPipelineRunner — loop control
# ---------------------------------------------------------------------------


def test_runner_returns_approved_on_first_pass(tmp_path: pathlib.Path) -> None:
    spec = tmp_path / "commission.md"
    spec.write_text("design X")
    reviewer = _ScriptedReviewer(_approved("good first try"))
    runner = DesignPipelineRunner(reviewer=reviewer)
    result = runner.run(spec)
    assert result.verdict == "approved"
    assert result.iteration_count == 1
    assert result.summary == "good first try"
    # Single review call.
    assert len(reviewer.calls) == 1


def test_runner_loops_then_approves(tmp_path: pathlib.Path) -> None:
    spec = tmp_path / "commission.md"
    spec.write_text("design Y")
    reviewer = _ScriptedReviewer(
        _needs_revision("round 1 issues"),
        _approved("round 2 fixed"),
    )
    # Pass _NullReviser explicitly so the test stays kernel-free even
    # though the production default is now _QwenReviser.
    runner = DesignPipelineRunner(reviewer=reviewer, reviser=_NullReviser())
    result = runner.run(spec)
    assert result.verdict == "approved"
    assert result.iteration_count == 2
    assert result.final_round == 2


def test_runner_caps_at_three_iterations(tmp_path: pathlib.Path) -> None:
    spec = tmp_path / "commission.md"
    spec.write_text("design Z")
    reviewer = _ScriptedReviewer(
        _needs_revision("r1", n=2),
        _needs_revision("r2", n=1),
        _needs_revision("r3", n=1),
    )
    runner = DesignPipelineRunner(reviewer=reviewer, reviser=_NullReviser())
    result = runner.run(spec)
    assert result.verdict == "cap_hit"
    assert result.iteration_count == 3
    assert result.last_feedback  # carries the unresolved issues
    assert "[critical]" not in result.summary  # summary is the model's text


def test_runner_reviser_invoked_only_after_first_iteration(
    tmp_path: pathlib.Path,
) -> None:
    spec = tmp_path / "commission.md"
    spec.write_text("design Q")
    seen_drafts: list[str] = []

    def fake_reviser(*, spec, draft, feedback):
        seen_drafts.append(draft)
        return draft + "\n\n[revised]"

    reviewer = _ScriptedReviewer(
        _needs_revision("r1"),
        _approved("ok"),
    )
    runner = DesignPipelineRunner(reviewer=reviewer, reviser=fake_reviser)
    result = runner.run(spec)
    # Reviser fired exactly once between iterations 1 and 2.
    assert len(seen_drafts) == 1
    assert "[revised]" in result.draft


# ---------------------------------------------------------------------------
# build_revision_prompt
# ---------------------------------------------------------------------------


def test_build_revision_prompt_carries_three_inputs() -> None:
    prompt = build_revision_prompt(
        spec="SPEC TEXT",
        draft="DRAFT TEXT",
        feedback=[
            {
                "category": "problem_solving",
                "severity": "critical",
                "description": "missed the point",
                "location": "section 1",
            }
        ],
    )
    assert "SPEC TEXT" in prompt
    assert "DRAFT TEXT" in prompt
    assert "[critical] problem_solving: missed the point" in prompt
    assert "Return the complete revised draft" in prompt


# ---------------------------------------------------------------------------
# commit_approved_draft + write_surface
# ---------------------------------------------------------------------------


def test_commit_approved_draft_writes_to_research(tmp_path: pathlib.Path) -> None:
    out = commit_approved_draft(
        tmp_path, draft="hello world", slug_hint="My_Cool Idea"
    )
    assert out.parent == tmp_path / "cortex-memory" / "research"
    assert out.read_text() == "hello world"
    # Slug sanitization: lowercase + hyphenated.
    assert "my-cool-idea" in out.name


def test_write_surface_uses_inner_surface_dir(tmp_path: pathlib.Path) -> None:
    out = write_surface(
        tmp_path,
        surface_type="design-commission-result",
        body="approved",
        extra_frontmatter={"verdict": "approved", "iterations": 2},
    )
    assert out.parent == tmp_path / "inner" / "surface"
    text = out.read_text()
    assert "type: design-commission-result" in text
    assert "verdict: approved" in text
    assert "iterations: 2" in text
    assert "approved" in text


# ---------------------------------------------------------------------------
# telemetry_payload
# ---------------------------------------------------------------------------


def test_telemetry_payload_shape() -> None:
    res = PipelineResult(
        iteration_count=2,
        verdict="approved",
        final_round=2,
        draft="d",
        summary="s",
        duration_seconds=12.5,
    )
    out = telemetry_payload(res, phase_value="design_commission")
    assert out == {
        "task_type": "design-commission",
        "phase": "design_commission",
        "iteration_count": 2,
        "verdict": "approved",
        "final_round": 2,
        "total_wake_seconds": 12.5,
    }


# ---------------------------------------------------------------------------
# _QwenReviser — failure-mode handling + PhaseRunner dispatch
# ---------------------------------------------------------------------------


# Long-enough draft that the 50%/75% ratios produce non-trivial cutoffs.
_DRAFT = "x" * 1000
_FEEDBACK = [
    {
        "category": "problem_solving",
        "severity": "critical",
        "description": "missed the point",
        "location": "section 1",
    },
    {
        "category": "layer_boundaries",
        "severity": "major",
        "description": "leaky boundary",
        "location": "section 2",
    },
]


def _stub_phase_runner(prompt: str = "PROMPT") -> MagicMock:
    """Return a Mock that mimics PhaseRunner.run -> (prompt, spec)."""
    runner = MagicMock()
    spec = MagicMock(name="KernelSpec")
    runner.run = MagicMock(return_value=(prompt, spec))
    return runner


def _make_reviser(
    kernel_outputs,
    *,
    phase_runner=None,
) -> _QwenReviser:
    """Build a _QwenReviser whose ``_execute_kernel_call`` is a stub.

    ``kernel_outputs`` is either a single value (returned every call) or
    a list yielded one-per-call. Values may be strings, exceptions, or
    callables (called with no args to produce the result).
    """
    runner = phase_runner or _stub_phase_runner()
    reviser = _QwenReviser(
        backend_spec=object(),
        phase_runner=runner,
        wake_context=object(),
    )

    if not isinstance(kernel_outputs, list):
        kernel_outputs = [kernel_outputs]
    queue = list(kernel_outputs)

    def fake_execute(prompt_text, kernel_spec):
        if not queue:
            raise RuntimeError("kernel mock ran out of responses")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    reviser._execute_kernel_call = fake_execute  # type: ignore[assignment]
    return reviser


def test_qwen_reviser_returns_revised_draft_on_ok() -> None:
    revised = "y" * 1100  # > 75% of input draft
    reviser = _make_reviser([revised])
    out = reviser(spec="SPEC", draft=_DRAFT, feedback=_FEEDBACK)
    assert out == revised
    assert len(reviser.events) == 1
    event = reviser.events[0]
    assert event["verdict"] == "ok"
    assert event["type"] == "design_commission_revision"
    assert event["draft_input_chars"] == len(_DRAFT)
    assert event["draft_output_chars"] == len(revised)
    assert event["feedback_count"] == 2
    assert event["critical_count"] == 1
    assert event["major_count"] == 1


def test_qwen_reviser_returns_input_draft_on_timeout() -> None:
    reviser = _make_reviser([asyncio.TimeoutError()])
    out = reviser(spec="SPEC", draft=_DRAFT, feedback=_FEEDBACK)
    assert out == _DRAFT
    assert len(reviser.events) == 1
    assert reviser.events[0]["verdict"] == "timeout"


def test_qwen_reviser_returns_input_draft_on_malformed() -> None:
    # 3 chars vs a 1000-char draft → ratio 0.003, well below the
    # 50% non-retryable threshold.
    reviser = _make_reviser(["err"])
    out = reviser(spec="SPEC", draft=_DRAFT, feedback=_FEEDBACK)
    assert out == _DRAFT
    assert len(reviser.events) == 1
    assert reviser.events[0]["verdict"] == "malformed"


def test_qwen_reviser_retries_once_on_short_output() -> None:
    # First call: 600 chars (between 50% and 75%) → short_output.
    # Second call: 900 chars (>= 75%) → ok.
    short_text = "s" * 600
    full_text = "f" * 900
    reviser = _make_reviser([short_text, full_text])
    out = reviser(spec="SPEC", draft=_DRAFT, feedback=_FEEDBACK)
    assert out == full_text
    assert len(reviser.events) == 2
    assert reviser.events[0]["verdict"] == "short_output"
    assert reviser.events[0]["attempt"] == 1
    assert reviser.events[1]["verdict"] == "ok"
    assert reviser.events[1]["attempt"] == 2


def test_qwen_reviser_falls_back_to_input_after_failed_retry() -> None:
    # Both attempts produce short output → return input draft unchanged.
    short_text_1 = "s" * 600
    short_text_2 = "t" * 700
    reviser = _make_reviser([short_text_1, short_text_2])
    out = reviser(spec="SPEC", draft=_DRAFT, feedback=_FEEDBACK)
    assert out == _DRAFT  # Input draft, not the short output.
    assert len(reviser.events) == 2
    assert reviser.events[0]["verdict"] == "short_output"
    assert reviser.events[1]["verdict"] == "short_output"


def test_qwen_reviser_uses_phase_revise_dispatch() -> None:
    runner = _stub_phase_runner(prompt="COMPOSED PROMPT")
    revised = "z" * 1100
    reviser = _QwenReviser(
        backend_spec=object(),
        phase_runner=runner,
        wake_context=object(),
    )
    reviser._execute_kernel_call = lambda prompt, spec: revised  # type: ignore[assignment]

    out = reviser(spec="SPEC", draft=_DRAFT, feedback=_FEEDBACK)
    assert out == revised

    # PhaseRunner.run was invoked exactly once with Phase.REVISE.
    assert runner.run.call_count == 1
    args, kwargs = runner.run.call_args
    # First positional arg is the phase.
    assert args[0] == Phase.REVISE
    # injected_content carries the composed revision prompt — must
    # include the spec, draft, and the formatted feedback summary.
    injected = kwargs.get("injected_content")
    assert injected is not None
    assert "SPEC" in injected
    assert _DRAFT in injected
    assert "missed the point" in injected


# ---------------------------------------------------------------------------
# Integration: full pipeline loop with a Qwen-style reviser.
# ---------------------------------------------------------------------------


def test_design_pipeline_loop_iterates_with_qwen_reviser(
    tmp_path: pathlib.Path,
) -> None:
    """Iteration 1 flagged → Qwen revises → iteration 2 approves.

    Verifies the full Sonnet → Qwen → Sonnet loop with a Qwen-style
    reviser injected at the runner's ``reviser=`` seam.
    """
    spec_path = tmp_path / "commission.md"
    spec_path.write_text("design X")

    reviewer = _ScriptedReviewer(
        _needs_revision("round 1 issues"),
        _approved("round 2 fixed"),
    )

    fixed_draft = "FIXED DRAFT" + "y" * 1000
    runner_mock = _stub_phase_runner()
    reviser = _QwenReviser(
        backend_spec=object(),
        phase_runner=runner_mock,
        wake_context=object(),
    )
    reviser._execute_kernel_call = lambda p, s: fixed_draft  # type: ignore[assignment]

    runner = DesignPipelineRunner(reviewer=reviewer, reviser=reviser)
    result = runner.run(spec_path)

    assert result.verdict == "approved"
    assert result.iteration_count == 2
    # The final draft is the one the Qwen reviser produced (Sonnet
    # approved on iteration 2 reviewing the revised draft).
    assert result.draft == fixed_draft
    # The reviser fired exactly once between iterations 1 and 2.
    assert len(reviser.events) == 1
    assert reviser.events[0]["verdict"] == "ok"


def test_design_pipeline_cap_hit_when_qwen_keeps_failing(
    tmp_path: pathlib.Path,
) -> None:
    """Sonnet flags every iteration + Qwen always times out → cap_hit.

    The reviser returns the input draft unchanged on each call (timeout
    path), so the pipeline never converges and cap-hits at the
    iteration limit. No commit is made by the runner; the wake.py
    layer would emit a cap-hit surface.
    """
    spec_path = tmp_path / "commission.md"
    spec_path.write_text("design Z")

    reviewer = _ScriptedReviewer(
        _needs_revision("r1", n=2),
        _needs_revision("r2", n=1),
        _needs_revision("r3", n=1),
    )

    runner_mock = _stub_phase_runner()
    reviser = _QwenReviser(
        backend_spec=object(),
        phase_runner=runner_mock,
        wake_context=object(),
    )
    # Every kernel call times out → reviser returns input draft.
    reviser._execute_kernel_call = lambda p, s: (_ for _ in ()).throw(  # type: ignore[assignment]
        asyncio.TimeoutError()
    )

    runner = DesignPipelineRunner(reviewer=reviewer, reviser=reviser)
    result = runner.run(spec_path)

    assert result.verdict == "cap_hit"
    assert result.iteration_count == 3
    # The reviser fired between iterations 1→2 and 2→3, total 2 calls.
    assert len(reviser.events) == 2
    assert all(e["verdict"] == "timeout" for e in reviser.events)
