"""Tests for ``alice_thinking.stage_d_judges``.

Mocks both call sites (``_call_qwen`` and ``_call_haiku``) so no live LLM
is hit. Exercises:

- clean ship verdict (Qwen + Haiku)
- clean reject verdict
- malformed JSON raises :class:`JudgeOutputError`
- ```json fences are tolerated
- bias-compensation prompt diffs are present in the prompt strings
"""

from __future__ import annotations

import pytest

from alice_thinking import stage_d_judges
from alice_thinking.stage_d_judges import (
    HAIKU_JUDGE_PROMPT_TEMPLATE,
    JudgeOutputError,
    QWEN_JUDGE_PROMPT_TEMPLATE,
    judge_haiku,
    judge_qwen,
)


# ---------------------------------------------------------------------------
# Fixtures — small canned source/synthesis bundle
# ---------------------------------------------------------------------------


SOURCE_A = "Source A: a research note about training periodization."
SOURCE_B = "Source B: a research note about LLM checkpoint cadence."
SYNTHESIS = "Both domains rely on the same recovery-vs-fatigue trade-off."


# ---------------------------------------------------------------------------
# judge_qwen
# ---------------------------------------------------------------------------


def test_judge_qwen_clean_ship(monkeypatch):
    captured = {}

    def fake_call_qwen(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            '{"tier": "T1", "novel": true, '
            '"reason": "Cross-domain insight changes how to plan checkpoint cadence.", '
            '"decision": "ship"}'
        )

    monkeypatch.setattr(stage_d_judges, "_call_qwen", fake_call_qwen)

    verdict = judge_qwen(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis=None,
    )

    assert verdict["tier"] == "T1"
    assert verdict["novel"] is True
    assert verdict["decision"] == "ship"
    assert "checkpoint cadence" in verdict["reason"]
    # Synthesis + sources made it into the prompt.
    assert SYNTHESIS in captured["prompt"]
    assert SOURCE_A in captured["prompt"]
    assert SOURCE_B in captured["prompt"]


def test_judge_qwen_clean_reject(monkeypatch):
    def fake_call_qwen(prompt: str) -> str:
        return (
            '{"tier": "T3", "novel": false, '
            '"reason": "Connection holds only at meta-level.", '
            '"decision": "reject"}'
        )

    monkeypatch.setattr(stage_d_judges, "_call_qwen", fake_call_qwen)

    verdict = judge_qwen(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis="prior synth body",
    )

    assert verdict["tier"] == "T3"
    assert verdict["novel"] is False
    assert verdict["decision"] == "reject"


def test_judge_qwen_malformed_json_raises(monkeypatch):
    def fake_call_qwen(prompt: str) -> str:
        return "this is not json at all"

    monkeypatch.setattr(stage_d_judges, "_call_qwen", fake_call_qwen)

    with pytest.raises(JudgeOutputError):
        judge_qwen(
            synthesis=SYNTHESIS,
            source_a_text=SOURCE_A,
            source_b_text=SOURCE_B,
            prior_pair_synthesis=None,
        )


def test_judge_qwen_empty_response_raises(monkeypatch):
    monkeypatch.setattr(stage_d_judges, "_call_qwen", lambda p: "")
    with pytest.raises(JudgeOutputError):
        judge_qwen(
            synthesis=SYNTHESIS,
            source_a_text=SOURCE_A,
            source_b_text=SOURCE_B,
            prior_pair_synthesis=None,
        )


def test_judge_qwen_invalid_tier_raises(monkeypatch):
    monkeypatch.setattr(
        stage_d_judges,
        "_call_qwen",
        lambda p: '{"tier": "T9", "novel": true, "reason": "x", "decision": "ship"}',
    )
    with pytest.raises(JudgeOutputError):
        judge_qwen(
            synthesis=SYNTHESIS,
            source_a_text=SOURCE_A,
            source_b_text=SOURCE_B,
            prior_pair_synthesis=None,
        )


def test_judge_qwen_json_fences_tolerated(monkeypatch):
    fenced = (
        "```json\n"
        '{"tier": "T2", "novel": true, '
        '"reason": "Predictable but useful link.", '
        '"decision": "ship"}\n'
        "```"
    )
    monkeypatch.setattr(stage_d_judges, "_call_qwen", lambda p: fenced)

    verdict = judge_qwen(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis=None,
    )

    assert verdict["tier"] == "T2"
    assert verdict["decision"] == "ship"


def test_judge_qwen_yes_no_string_novel_accepted(monkeypatch):
    """The schema doc shows ``yes``/``no`` strings; the design uses bool.
    Accept either to be tolerant of model drift."""
    monkeypatch.setattr(
        stage_d_judges,
        "_call_qwen",
        lambda p: '{"tier": "T1", "novel": "yes", "reason": "x", "decision": "ship"}',
    )
    verdict = judge_qwen(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis=None,
    )
    assert verdict["novel"] is True


# ---------------------------------------------------------------------------
# judge_haiku
# ---------------------------------------------------------------------------


def test_judge_haiku_clean_ship(monkeypatch):
    captured = {}

    def fake_call_haiku(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            '{"tier": "T1", "novel": true, '
            '"reason": "Skeptical pass: connection survives operational scrutiny.", '
            '"decision": "ship"}'
        )

    monkeypatch.setattr(stage_d_judges, "_call_haiku", fake_call_haiku)

    verdict = judge_haiku(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis=None,
    )

    assert verdict["tier"] == "T1"
    assert verdict["decision"] == "ship"
    assert SYNTHESIS in captured["prompt"]


def test_judge_haiku_clean_reject(monkeypatch):
    monkeypatch.setattr(
        stage_d_judges,
        "_call_haiku",
        lambda p: (
            '{"tier": "T2", "novel": false, '
            '"reason": "Same connection synthesised previously.", '
            '"decision": "reject"}'
        ),
    )

    verdict = judge_haiku(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis="some prior synth",
    )

    assert verdict["tier"] == "T2"
    assert verdict["novel"] is False
    assert verdict["decision"] == "reject"


def test_judge_haiku_malformed_raises(monkeypatch):
    monkeypatch.setattr(stage_d_judges, "_call_haiku", lambda p: "garbage {")

    with pytest.raises(JudgeOutputError):
        judge_haiku(
            synthesis=SYNTHESIS,
            source_a_text=SOURCE_A,
            source_b_text=SOURCE_B,
            prior_pair_synthesis=None,
        )


def test_judge_haiku_json_fences_tolerated(monkeypatch):
    fenced = (
        "```\n"
        '{"tier": "T4", "novel": false, "reason": "no actionable structure.", "decision": "reject"}\n'
        "```"
    )
    monkeypatch.setattr(stage_d_judges, "_call_haiku", lambda p: fenced)

    verdict = judge_haiku(
        synthesis=SYNTHESIS,
        source_a_text=SOURCE_A,
        source_b_text=SOURCE_B,
        prior_pair_synthesis=None,
    )

    assert verdict["tier"] == "T4"


def test_judge_haiku_missing_field_raises(monkeypatch):
    monkeypatch.setattr(
        stage_d_judges,
        "_call_haiku",
        lambda p: '{"tier": "T1", "novel": true, "decision": "ship"}',  # no reason
    )

    with pytest.raises(JudgeOutputError):
        judge_haiku(
            synthesis=SYNTHESIS,
            source_a_text=SOURCE_A,
            source_b_text=SOURCE_B,
            prior_pair_synthesis=None,
        )


# ---------------------------------------------------------------------------
# Bias-compensation prompt diffs
# ---------------------------------------------------------------------------


def test_qwen_prompt_damps_verbosity():
    """Qwen needs format restriction + permission to reject — the
    'rejection is a valid and useful output' framing is the explicit
    bias-compensation anchor.
    """
    assert "rejection is a valid and useful output" in QWEN_JUDGE_PROMPT_TEMPLATE
    assert "do not penalize yourself for\nsaying reject" in QWEN_JUDGE_PROMPT_TEMPLATE
    # No-preamble + one-sentence constraint.
    assert "ONE sentence" in QWEN_JUDGE_PROMPT_TEMPLATE
    assert "No preamble" in QWEN_JUDGE_PROMPT_TEMPLATE


def test_haiku_prompt_invites_skepticism():
    """Haiku needs skepticism training to override agreeableness.
    'Default to skepticism' + 'careful rejection is better than a lazy
    acceptance' are the bias-compensation anchors.
    """
    assert "thorough and skeptical" in HAIKU_JUDGE_PROMPT_TEMPLATE
    assert "careful rejection is better than a lazy acceptance" in HAIKU_JUDGE_PROMPT_TEMPLATE
    assert "Default to skepticism" in HAIKU_JUDGE_PROMPT_TEMPLATE
    assert "Finding reasons to reject is your specialty" in HAIKU_JUDGE_PROMPT_TEMPLATE


def test_qwen_and_haiku_prompts_differ():
    """Same rubric, different prompt text — confirms one-rubric / two-prompts
    is wired correctly (not a copy-paste regression)."""
    assert QWEN_JUDGE_PROMPT_TEMPLATE != HAIKU_JUDGE_PROMPT_TEMPLATE
    # Both share the rubric body.
    for tier_line in (
        "T1 — Non-obvious connection",
        "T2 — Real but predictable",
        "T3 — Forced or abstract-level",
        "T4 — Null result",
    ):
        assert tier_line in QWEN_JUDGE_PROMPT_TEMPLATE
        assert tier_line in HAIKU_JUDGE_PROMPT_TEMPLATE


def test_judge_haiku_missing_api_key_raises(monkeypatch):
    """Without ANTHROPIC_API_KEY the haiku judge raises before hitting
    the SDK. This is a defensive contract — caller can catch and fall
    back."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Neutralize auth-env resolution so a host-side ``alice.env`` can't
    # repopulate the key out from under the test's premise.
    monkeypatch.setattr(stage_d_judges, "ensure_auth_env", lambda *a, **kw: None)
    # Don't monkeypatch _call_haiku — we want the real one to short-circuit
    # on the missing-key check.
    with pytest.raises(JudgeOutputError):
        judge_haiku(
            synthesis=SYNTHESIS,
            source_a_text=SOURCE_A,
            source_b_text=SOURCE_B,
            prior_pair_synthesis=None,
        )
