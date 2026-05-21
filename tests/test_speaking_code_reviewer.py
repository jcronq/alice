"""Tests for ``alice_speaking.review.code_reviewer``.

Pin: JSON-output contract (verdict gate, severity taxonomy, category
whitelist) and parser robustness (fence stripping, malformed-JSON
rejection). Mirrors
``tests/test_thinking_design_pipeline.py``'s ``ReviewResult`` tests but
exercises the code-quality category whitelist instead of the design
reviewer's.
"""

from __future__ import annotations

import json

import pytest

from alice_speaking.review import (
    CODE_REVIEW_CATEGORIES,
    CODE_REVIEWER_SYSTEM_PROMPT,
    CodeReviewResult,
)
from forge import dispatcher as sm


# ---------------------------------------------------------------------------
# System-prompt contract
# ---------------------------------------------------------------------------


def test_categories_match_r3_recommendation() -> None:
    """The whitelist is fixed by SWE-practices R3 — pin it explicitly."""
    assert CODE_REVIEW_CATEGORIES == frozenset(
        {
            "test_adequacy",
            "security",
            "performance",
            "error_handling",
            "naming_and_clarity",
            "requirements_coverage",
        }
    )


def test_system_prompt_lists_every_whitelisted_category() -> None:
    """The reviewer can only flag categories it's told it may use."""
    for category in CODE_REVIEW_CATEGORIES:
        assert category in CODE_REVIEWER_SYSTEM_PROMPT, (
            f"system prompt missing {category!r} — the reviewer would never "
            f"learn this is a legal category"
        )


def test_system_prompt_enforces_strict_json_no_fences() -> None:
    """Procedural-output enforcement — feedback_procedural_logic_in_code."""
    assert "STRICT JSON" in CODE_REVIEWER_SYSTEM_PROMPT
    assert "No prose, no markdown fences." in CODE_REVIEWER_SYSTEM_PROMPT


def test_system_prompt_pins_severity_taxonomy() -> None:
    """Severity gate matches the design reviewer for dispatcher parity."""
    for severity in ("critical", "major", "minor"):
        assert severity in CODE_REVIEWER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Verdict parsing — approval / needs_revision paths
# ---------------------------------------------------------------------------


def test_parse_json_approval_verdict() -> None:
    payload = {
        "verdict": "approved",
        "confidence": 0.92,
        "summary": "tests cover the change, no security concerns",
        "feedback": [],
        "patterns": [],
    }
    res = CodeReviewResult.parse_json(json.dumps(payload))
    assert res.verdict == "approved"
    assert res.confidence == pytest.approx(0.92)
    assert res.summary == "tests cover the change, no security concerns"
    assert res.feedback == []
    assert res.patterns == []


def test_parse_json_needs_revision_with_critical_feedback() -> None:
    payload = {
        "verdict": "needs_revision",
        "confidence": 0.7,
        "summary": "security regression on auth path",
        "feedback": [
            {
                "category": "security",
                "severity": "critical",
                "description": "session token logged in plaintext",
                "location": "src/alice_speaking/auth.py",
            },
            {
                "category": "test_adequacy",
                "severity": "critical",
                "description": "no regression test for the fixed bug",
                "location": "tests/",
            },
        ],
        "patterns": ["missing-regression-test"],
    }
    res = CodeReviewResult.parse_json(json.dumps(payload))
    assert res.verdict == "needs_revision"
    assert len(res.feedback) == 2
    severities = {f["severity"] for f in res.feedback}
    assert severities == {"critical"}
    categories = {f["category"] for f in res.feedback}
    assert categories == {"security", "test_adequacy"}
    assert res.patterns == ["missing-regression-test"]


def test_parse_json_needs_revision_with_major_feedback() -> None:
    payload = {
        "verdict": "needs_revision",
        "confidence": 0.55,
        "summary": "performance regression on hot path",
        "feedback": [
            {
                "category": "performance",
                "severity": "major",
                "description": "added N+1 query in the request handler",
                "location": "src/alice_speaking/turn_runner.py",
            },
            {
                "category": "error_handling",
                "severity": "major",
                "description": "bare except swallows kernel timeouts",
                "location": "src/alice_speaking/turn_runner.py",
            },
        ],
    }
    res = CodeReviewResult.parse_json(json.dumps(payload))
    assert res.verdict == "needs_revision"
    assert len(res.feedback) == 2
    assert all(f["severity"] == "major" for f in res.feedback)


def test_parse_json_strips_markdown_fences() -> None:
    """Models occasionally wrap JSON in ```json fences despite instructions."""
    body = (
        "```json\n"
        '{"verdict": "approved", "confidence": 0.8, "summary": "ok"}\n'
        "```"
    )
    res = CodeReviewResult.parse_json(body)
    assert res.verdict == "approved"


# ---------------------------------------------------------------------------
# Malformed-JSON rejection
# ---------------------------------------------------------------------------


def test_parse_json_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        CodeReviewResult.parse_json("not json at all{")


def test_parse_json_rejects_non_object_root() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        CodeReviewResult.parse_json('["approved", "ok"]')


def test_parse_json_rejects_unknown_verdict() -> None:
    with pytest.raises(ValueError, match="unexpected verdict"):
        CodeReviewResult.parse_json(
            '{"verdict": "maybe", "confidence": 0.5, "summary": "x"}'
        )


def test_parse_json_rejects_missing_verdict() -> None:
    with pytest.raises(ValueError, match="unexpected verdict"):
        CodeReviewResult.parse_json('{"confidence": 0.5, "summary": "x"}')


# ---------------------------------------------------------------------------
# Category whitelist enforcement at the parse boundary
# ---------------------------------------------------------------------------


def test_parse_json_drops_off_whitelist_categories() -> None:
    """The system prompt forbids inventing categories — the parser enforces it."""
    payload = {
        "verdict": "needs_revision",
        "confidence": 0.6,
        "summary": "mixed bag",
        "feedback": [
            {
                "category": "security",
                "severity": "critical",
                "description": "real issue",
                "location": "global",
            },
            {
                "category": "vibes",  # not a real category
                "severity": "major",
                "description": "model invented this",
                "location": "global",
            },
            {
                "category": "problem_solving",  # design-reviewer category
                "severity": "major",
                "description": "wrong reviewer's category",
                "location": "global",
            },
        ],
    }
    res = CodeReviewResult.parse_json(json.dumps(payload))
    assert len(res.feedback) == 1
    assert res.feedback[0]["category"] == "security"


def test_parse_json_drops_non_dict_feedback_entries() -> None:
    payload = {
        "verdict": "needs_revision",
        "confidence": 0.5,
        "summary": "x",
        "feedback": [
            "not-a-dict",
            {
                "category": "naming_and_clarity",
                "severity": "minor",
                "description": "fine",
                "location": "global",
            },
        ],
    }
    res = CodeReviewResult.parse_json(json.dumps(payload))
    assert len(res.feedback) == 1
    assert res.feedback[0]["category"] == "naming_and_clarity"


# ---------------------------------------------------------------------------
# SPAWN_MAP wiring — issue #107 done-when
# ---------------------------------------------------------------------------


def test_spawn_map_has_reviewing_code_entry() -> None:
    """The dispatcher integration point lives in sm.SPAWN_MAP."""
    assert ("sm:reviewing", "art:code") in sm.SPAWN_MAP
    entry = sm.SPAWN_MAP[("sm:reviewing", "art:code")]
    # The entry references the new reviewer's system prompt by dotted
    # path so the future dispatcher loader can resolve it without
    # sm taking a hard import on alice_speaking.
    assert (
        entry["system_prompt_module"]
        == "alice_speaking.review.code_reviewer:CODE_REVIEWER_SYSTEM_PROMPT"
    )
    assert entry["system_prompt_role"] == "code-reviewer"


def test_spawn_map_preserves_existing_selected_entries() -> None:
    """The tuple-key rework must not have dropped any v1 worker rows."""
    for art in ("art:code", "art:config_change", "art:research_note", "art:experiment"):
        assert ("sm:selected", art) in sm.SPAWN_MAP, (
            f"missing v1 worker spawn config for {art}"
        )
