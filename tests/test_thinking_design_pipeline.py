"""Tests for ``alice_thinking.design_pipeline``.

Pin: loop control + verdict-driven side effects (commit on
approved, no commit on cap-hit), structured-output parsing,
revision-prompt composition, surface emission shape.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_thinking.design_pipeline import (
    DesignPipelineRunner,
    PipelineResult,
    ReviewResult,
    SubAgentRunner,
    build_revision_prompt,
    commit_approved_draft,
    telemetry_payload,
    write_surface,
)


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
    runner = DesignPipelineRunner(reviewer=reviewer)
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
    runner = DesignPipelineRunner(reviewer=reviewer)
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
