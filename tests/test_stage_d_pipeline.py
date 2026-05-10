"""Tests for ``alice_thinking.stage_d_pipeline``.

Mocks ``judge_qwen``/``judge_haiku`` and ``draft_synthesis_fn`` so no
live LLM is hit. Exercises:

- both-ship → outcome ``shipped``, exactly 1 JSONL line
- both-reject (agreement on reject) → outcome ``dropped_agreement_reject``,
  exactly 1 JSONL line
- disagreement on attempt 1, both-ship on attempt 2 → outcome ``shipped``,
  exactly 2 JSONL lines, retry_history populated
- 3-attempt persistent disagreement → outcome
  ``dropped_disagreement_exhausted``, exactly 3 lines
- JSONL line shape matches ``src/alice_viewer/STAGE_D_SCHEMA.md`` field-for-field
- ``update_shipped_slug`` rewrites the most recent matching line in place
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_thinking.stage_d_pipeline import (
    run_dual_judge,
    update_shipped_slug,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ship_verdict(tier="T1", reason="strong cross-domain link") -> dict:
    return {"tier": tier, "novel": True, "reason": reason, "decision": "ship"}


def _reject_verdict(tier="T3", reason="forced abstract-only") -> dict:
    return {"tier": tier, "novel": False, "reason": reason, "decision": "reject"}


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_drafter(drafts):
    """Return a callable that yields successive drafts on each call.
    ``drafts`` is a list — first attempt gets drafts[0], second gets
    drafts[1], etc."""
    state = {"i": 0}
    captured: list = []

    def fn(prior):
        captured.append(prior)
        i = state["i"]
        state["i"] += 1
        if i >= len(drafts):
            raise AssertionError(f"drafter called {i + 1} times; only {len(drafts)} prepared")
        return drafts[i]

    fn.captured_priors = captured  # type: ignore[attr-defined]
    return fn


# ---------------------------------------------------------------------------
# Both-ship: one attempt, outcome=shipped
# ---------------------------------------------------------------------------


def test_both_ship_writes_one_line_outcome_shipped(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["Synthesis text 1"])

    qwen_calls = []
    haiku_calls = []

    def qwen(**kw):
        qwen_calls.append(kw)
        return _ship_verdict()

    def haiku(**kw):
        haiku_calls.append(kw)
        return _ship_verdict()

    rec = run_dual_judge(
        slug_a="research/a",
        slug_b="people/b",
        source_a_text="A body",
        source_b_text="B body",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=qwen,
        judge_haiku_fn=haiku,
    )

    assert rec.outcome == "shipped"
    assert rec.draft_attempt_n == 1
    assert rec.synthesis_text == "Synthesis text 1"
    assert rec.retry_history == []
    assert rec.qwen_verdict["decision"] == "ship"
    assert rec.haiku_verdict["decision"] == "ship"

    lines = _read_jsonl(log)
    assert len(lines) == 1
    line = lines[0]
    assert line["outcome"] == "shipped"
    assert line["draft_attempt_n"] == 1
    assert line["pair"] == {"slug_a": "research/a", "slug_b": "people/b"}
    assert line["synthesis_text"] == "Synthesis text 1"
    assert line["shipped_slug"] is None
    assert len(qwen_calls) == 1 and len(haiku_calls) == 1


# ---------------------------------------------------------------------------
# Both-reject: one attempt, outcome=dropped_agreement_reject
# ---------------------------------------------------------------------------


def test_both_reject_writes_one_line_outcome_dropped(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["Synthesis 1"])

    rec = run_dual_judge(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: _reject_verdict(),
        judge_haiku_fn=lambda **kw: _reject_verdict(),
    )

    assert rec.outcome == "dropped_agreement_reject"
    assert rec.draft_attempt_n == 1
    assert rec.retry_history == []

    lines = _read_jsonl(log)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "dropped_agreement_reject"


# ---------------------------------------------------------------------------
# Disagreement, then ship on attempt 2
# ---------------------------------------------------------------------------


def test_disagreement_then_ship_two_lines(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["Synthesis v1", "Synthesis v2 (revised)"])

    qwen_responses = iter([_ship_verdict(), _ship_verdict()])
    haiku_responses = iter([_reject_verdict(), _ship_verdict()])

    rec = run_dual_judge(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: next(qwen_responses),
        judge_haiku_fn=lambda **kw: next(haiku_responses),
    )

    assert rec.outcome == "shipped"
    assert rec.draft_attempt_n == 2
    assert rec.synthesis_text == "Synthesis v2 (revised)"
    assert rec.retry_history == ["Synthesis v1"]

    lines = _read_jsonl(log)
    assert len(lines) == 2
    assert lines[0]["outcome"] == "disagreement_pending"
    assert lines[0]["draft_attempt_n"] == 1
    assert lines[0]["synthesis_text"] == "Synthesis v1"
    assert lines[1]["outcome"] == "shipped"
    assert lines[1]["draft_attempt_n"] == 2
    assert lines[1]["synthesis_text"] == "Synthesis v2 (revised)"
    assert lines[1]["retry_history"] == ["Synthesis v1"]
    # Same attempt_id across both lines (one pipeline run = one attempt id).
    assert lines[0]["id"] == lines[1]["id"]
    # Drafter called once with None (first), once with "Synthesis v1" (second).
    assert drafter.captured_priors == [None, "Synthesis v1"]


# ---------------------------------------------------------------------------
# Persistent disagreement: 3 lines, outcome=dropped_disagreement_exhausted
# ---------------------------------------------------------------------------


def test_persistent_disagreement_exhausted_three_lines(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["draft 1", "draft 2", "draft 3"])

    # Qwen always ships, Haiku always rejects → never agree.
    qwen_responses = [_ship_verdict() for _ in range(3)]
    haiku_responses = [_reject_verdict() for _ in range(3)]

    rec = run_dual_judge(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: qwen_responses.pop(0),
        judge_haiku_fn=lambda **kw: haiku_responses.pop(0),
    )

    assert rec.outcome == "dropped_disagreement_exhausted"
    assert rec.draft_attempt_n == 3
    assert rec.synthesis_text == "draft 3"
    assert rec.retry_history == ["draft 1", "draft 2"]

    lines = _read_jsonl(log)
    assert len(lines) == 3
    assert lines[0]["outcome"] == "disagreement_pending"
    assert lines[1]["outcome"] == "disagreement_pending"
    assert lines[2]["outcome"] == "dropped_disagreement_exhausted"
    assert lines[2]["retry_history"] == ["draft 1", "draft 2"]


# ---------------------------------------------------------------------------
# Schema field-for-field
# ---------------------------------------------------------------------------


REQUIRED_SCHEMA_FIELDS = {
    "id",
    "pair",
    "synthesis_text",
    "draft_attempt_n",
    "qwen_verdict",
    "haiku_verdict",
    "outcome",
    "retry_history",
    "created_at",
    "shipped_slug",
}


def test_jsonl_line_matches_schema(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["s1"])

    run_dual_judge(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: _ship_verdict("T1", "qwen reason"),
        judge_haiku_fn=lambda **kw: _ship_verdict("T2", "haiku reason"),
    )

    line = _read_jsonl(log)[0]
    assert set(line.keys()) == REQUIRED_SCHEMA_FIELDS
    assert isinstance(line["id"], str) and line["id"].startswith("att-")
    assert line["pair"] == {"slug_a": "research/a", "slug_b": "research/b"}
    assert isinstance(line["synthesis_text"], str)
    assert line["draft_attempt_n"] == 1
    assert set(line["qwen_verdict"].keys()) == {"tier", "novel", "reason", "decision"}
    assert set(line["haiku_verdict"].keys()) == {"tier", "novel", "reason", "decision"}
    assert line["outcome"] == "shipped"
    assert line["retry_history"] == []
    # ISO 8601 with offset (e.g., 2026-05-09T03:14:22-04:00). Permissive
    # check — exact offset varies by host.
    assert "T" in line["created_at"]
    assert line["shipped_slug"] is None


# ---------------------------------------------------------------------------
# update_shipped_slug
# ---------------------------------------------------------------------------


def test_update_shipped_slug_rewrites_last_matching_line(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["s1"])

    rec = run_dual_judge(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: _ship_verdict(),
        judge_haiku_fn=lambda **kw: _ship_verdict(),
    )

    ok = update_shipped_slug(
        attempt_id=rec.id,
        shipped_slug="research/2026-05-09-test-synthesis",
        attempts_log_path=log,
    )
    assert ok

    lines = _read_jsonl(log)
    assert len(lines) == 1
    assert lines[0]["shipped_slug"] == "research/2026-05-09-test-synthesis"


def test_update_shipped_slug_returns_false_for_unknown_id(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["s1"])
    run_dual_judge(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: _ship_verdict(),
        judge_haiku_fn=lambda **kw: _ship_verdict(),
    )

    ok = update_shipped_slug(
        attempt_id="att-does-not-exist",
        shipped_slug="research/foo",
        attempts_log_path=log,
    )
    assert ok is False


def test_update_shipped_slug_missing_log_returns_false(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    ok = update_shipped_slug(
        attempt_id="att-anything",
        shipped_slug="research/foo",
        attempts_log_path=log,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Drafter contract: empty draft should raise
# ---------------------------------------------------------------------------


def test_empty_synthesis_raises(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    with pytest.raises(ValueError):
        run_dual_judge(
            slug_a="a", slug_b="b",
            source_a_text="A", source_b_text="B",
            draft_synthesis_fn=lambda prior: "",
            prior_pair_synthesis=None,
            attempts_log_path=log,
            judge_qwen_fn=lambda **kw: _ship_verdict(),
            judge_haiku_fn=lambda **kw: _ship_verdict(),
        )


# ---------------------------------------------------------------------------
# Per-attempt id is stable inside one pipeline run
# ---------------------------------------------------------------------------


def test_attempt_id_stable_across_attempts(tmp_path):
    log = tmp_path / "stage-d-attempts.jsonl"
    drafter = _make_drafter(["a", "b", "c"])
    qwen_responses = [_ship_verdict() for _ in range(3)]
    haiku_responses = [_reject_verdict() for _ in range(3)]

    run_dual_judge(
        slug_a="a", slug_b="b",
        source_a_text="A", source_b_text="B",
        draft_synthesis_fn=drafter,
        prior_pair_synthesis=None,
        attempts_log_path=log,
        judge_qwen_fn=lambda **kw: qwen_responses.pop(0),
        judge_haiku_fn=lambda **kw: haiku_responses.pop(0),
    )

    lines = _read_jsonl(log)
    ids = {line["id"] for line in lines}
    assert len(ids) == 1, "attempt_id should be the same across all lines in one run"
