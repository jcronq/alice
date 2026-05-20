"""End-to-end integration test for the speaking-benchmark.

Drives a tiny synthetic sample through ``instances → run → score``
with a fake candidate (no httpx network calls), verifies the
resulting pass-rate report.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from eval import bench
from eval.bench import select_subset
from eval.replay import ReplayResult


def _sample_row(turn_id: str, category: str, outbound: str) -> dict:
    return {
        "turn_id": turn_id,
        "sampled_category": category,
        "ts": 1779131000.0 + int(turn_id.split("_")[-1]),
        "sender_number": "+15553334444",
        "sender_name": "jason",
        "inbound": f"prompt for {turn_id}",
        "outbound": outbound,
    }


@pytest.fixture
def tiny_sample() -> list[dict]:
    return [
        _sample_row(
            "turn_1",
            "tactical",
            'send_message(recipient="jason", message="all green")',
        ),
        _sample_row(
            "turn_2",
            "tactical",
            'send_message(recipient="katie", message="see you soon")',
        ),
        _sample_row(
            "turn_3",
            "design",
            "Agent(prompt='build the thing') — worker dispatched",
        ),
    ]


@pytest.fixture
def sample_path(tmp_path: Path, tiny_sample: list[dict]) -> Path:
    path = tmp_path / "eval_sample.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in tiny_sample) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def log_path(tmp_path: Path, tiny_sample: list[dict]) -> Path:
    path = tmp_path / "speaking-turns.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in tiny_sample) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def candidates_path(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "id": "fake",
                        "label": "Fake (offline)",
                        "provider": "anthropic",
                        "model": "fake-model",
                        "auth_env": [],
                    }
                ]
            }
        )
    )
    return path


def test_instances_then_run_then_score(
    tmp_path: Path,
    sample_path: Path,
    log_path: Path,
    candidates_path: Path,
    tiny_sample: list[dict],
):
    instances_dir = tmp_path / "instances"

    # Step 1: derive assertion files
    bench._instances.main_instances(
        sample_path=sample_path,
        out_dir=instances_dir,
    )
    assert (instances_dir / "turn_1.assert.json").is_file()

    # Step 2: stub replay_turn so we don't hit the network. Echo each
    # turn's historical outbound back as the candidate's output. This
    # is a "perfect" candidate from the assertion runner's POV — every
    # assertion should pass.
    async def fake_replay_turn(candidate, sampled_turn, all_turns, **kw):
        return ReplayResult(
            turn_id=sampled_turn["turn_id"],
            candidate_id=candidate.id,
            category=sampled_turn.get("sampled_category", "unknown"),
            output=sampled_turn["outbound"],
            latency_ms=10,
            input_tokens=10,
            output_tokens=10,
            status="ok",
            error=None,
            request_ts=0.0,
        )

    with patch("eval.bench.replay_turn", side_effect=fake_replay_turn), \
         patch("eval.bench.build_system_prompt", return_value="sys-prompt"):
        out_path = tmp_path / "eval_results.jsonl"
        rows = bench.main_run(
            candidate_id="fake",
            sample_path=sample_path,
            candidates_path=candidates_path,
            instances_dir=instances_dir,
            out_path=out_path,
            log_path=log_path,
            subset="full",
        )

    assert len(rows) == 3
    assert all(r["resolved"] for r in rows), [
        (r["turn_id"], [a for a in r["assertions"] if not a["passed"]])
        for r in rows
        if not r["resolved"]
    ]

    # Step 3: scoring — perfect candidate → 100% pass-rate → ACCEPTABLE
    report = bench._score.main_score(
        results_path=out_path, out_path=None, candidate_id="fake"
    )
    assert report.aggregate_pass_rate == 1.0
    assert report.verdict == "ACCEPTABLE"


def test_bad_candidate_fails_assertions(
    tmp_path: Path,
    sample_path: Path,
    log_path: Path,
    candidates_path: Path,
):
    instances_dir = tmp_path / "instances"
    bench._instances.main_instances(
        sample_path=sample_path,
        out_dir=instances_dir,
    )

    async def fake_replay_turn(candidate, sampled_turn, all_turns, **kw):
        # Empty replies + forbidden tool — many assertions should fail
        return ReplayResult(
            turn_id=sampled_turn["turn_id"],
            candidate_id=candidate.id,
            category=sampled_turn.get("sampled_category", "unknown"),
            output="$ signal-cli -a +1 send -m 'nope'",
            latency_ms=10,
            input_tokens=10,
            output_tokens=10,
            status="ok",
            error=None,
            request_ts=0.0,
        )

    with patch("eval.bench.replay_turn", side_effect=fake_replay_turn), \
         patch("eval.bench.build_system_prompt", return_value="sys"):
        out_path = tmp_path / "eval_results.jsonl"
        bench.main_run(
            candidate_id="fake",
            sample_path=sample_path,
            candidates_path=candidates_path,
            instances_dir=instances_dir,
            out_path=out_path,
            log_path=log_path,
            subset="full",
        )

    report = bench._score.main_score(
        results_path=out_path, out_path=None, candidate_id="fake"
    )
    # Forbidden tool + missing send_message → all instances fail
    assert report.aggregate_pass_rate == 0.0
    assert report.verdict == "NEEDS_WORK"


class TestSelectSubset:
    def test_full_returns_all(self, tiny_sample):
        assert select_subset(tiny_sample, "full") == tiny_sample

    def test_lite_filters_by_category(self):
        rows = [
            _sample_row(f"turn_{i}", "tactical", "ok")
            for i in range(10)
        ] + [
            _sample_row(f"turn_{i + 100}", "design", "ok")
            for i in range(5)
        ] + [
            _sample_row(f"turn_{i + 200}", "image", "ok") for i in range(3)
        ]
        out = select_subset(rows, "lite")
        # Lite spec: 4 tactical + 2 design + 2 image + 2 conversational
        # (the conversational quota is unused here)
        by_cat = {}
        for r in out:
            by_cat[r["sampled_category"]] = by_cat.get(r["sampled_category"], 0) + 1
        assert by_cat.get("tactical") == 4
        assert by_cat.get("design") == 2
        assert by_cat.get("image") == 2
        assert sum(by_cat.values()) <= 10

    def test_verified_with_file(self, tmp_path, tiny_sample):
        verified = tmp_path / "verified.txt"
        verified.write_text("turn_1\n# comment\nturn_3\n")
        out = select_subset(tiny_sample, "verified", verified_path=verified)
        assert {r["turn_id"] for r in out} == {"turn_1", "turn_3"}

    def test_verified_missing_falls_through(self, tmp_path, tiny_sample):
        out = select_subset(
            tiny_sample,
            "verified",
            verified_path=tmp_path / "missing.txt",
        )
        assert out == tiny_sample

    def test_unknown_subset_raises(self, tiny_sample):
        with pytest.raises(ValueError):
            select_subset(tiny_sample, "wat")
