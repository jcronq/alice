"""Tests for the stratified-pass-rate scorer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alice_eval.score import (
    DECISION_THRESHOLD,
    format_report,
    main_score,
    score_results,
)


def _row(turn_id, category, candidate_id, resolved):
    return {
        "turn_id": turn_id,
        "category": category,
        "candidate_id": candidate_id,
        "resolved": resolved,
    }


class TestScoreResults:
    def test_empty_input(self):
        report = score_results([])
        assert report.aggregate_pass_rate == 0.0
        assert report.verdict == "NEEDS_WORK"

    def test_all_pass(self):
        rows = [
            _row("t1", "tactical", "opus", True),
            _row("t2", "design", "opus", True),
            _row("t3", "image", "opus", True),
        ]
        report = score_results(rows)
        assert report.aggregate_pass_rate == 1.0
        assert report.verdict == "ACCEPTABLE"
        assert report.by_category["tactical"].rate == 1.0

    def test_mixed(self):
        rows = [
            _row("t1", "tactical", "opus", True),
            _row("t2", "tactical", "opus", False),
            _row("t3", "design", "opus", True),
        ]
        report = score_results(rows)
        # 2/3 raw
        assert report.aggregate_pass_rate == pytest.approx(2 / 3)
        # tactical: 1/2; design: 1/1
        assert report.by_category["tactical"].rate == pytest.approx(0.5)
        assert report.by_category["design"].rate == 1.0

    def test_candidate_filter(self):
        rows = [
            _row("t1", "tactical", "opus", True),
            _row("t2", "tactical", "qwen", False),
            _row("t3", "tactical", "qwen", False),
        ]
        opus = score_results(rows, candidate_id="opus")
        qwen = score_results(rows, candidate_id="qwen")
        assert opus.aggregate_pass_rate == 1.0
        assert qwen.aggregate_pass_rate == 0.0
        assert opus.candidate_id == "opus"
        assert qwen.candidate_id == "qwen"

    def test_weighted_uses_design_targets(self):
        # tactical and design are weighted equally (10/40 each = 0.25);
        # together they account for 50% of weight. If both are perfect,
        # weighted rate should be 1.0 regardless of missing categories.
        rows = [
            _row(f"t{i}", "tactical", "opus", True) for i in range(10)
        ] + [
            _row(f"d{i}", "design", "opus", True) for i in range(10)
        ]
        report = score_results(rows)
        assert report.weighted_pass_rate == pytest.approx(1.0)

    def test_decision_threshold_boundary(self):
        # Cherry-pick: enough passes to put weighted right at threshold.
        rows = [
            _row(f"t{i}", "tactical", "opus", i < 8) for i in range(10)
        ]
        report = score_results(rows)
        # Tactical only → weighted = tactical rate = 0.8 ≥ 0.75
        assert report.verdict == "ACCEPTABLE"

    def test_format_report_smoke(self):
        rows = [
            _row("t1", "tactical", "opus", True),
            _row("t2", "design", "opus", False),
        ]
        report = score_results(rows)
        rendered = format_report(report)
        assert "alice-speak score: opus" in rendered
        assert "tactical" in rendered
        assert "verdict" in rendered

    def test_main_score_writes_json(self, tmp_path: Path, capsys):
        results_path = tmp_path / "eval_results.jsonl"
        rows = [
            _row("t1", "tactical", "opus", True),
            _row("t2", "design", "opus", False),
        ]
        results_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        out_path = tmp_path / "report.json"
        report = main_score(
            results_path=results_path,
            out_path=out_path,
            candidate_id="opus",
        )
        assert report.candidate_id == "opus"
        loaded = json.loads(out_path.read_text())
        assert "by_category" in loaded
        assert loaded["candidate_id"] == "opus"


def test_decision_threshold_constant():
    # Continuity sanity check: don't accidentally drift from the original
    # blind-A/B threshold.
    assert DECISION_THRESHOLD == 0.75
