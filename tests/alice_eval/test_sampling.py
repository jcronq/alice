"""Sampling unit tests — classification + stratification."""

from __future__ import annotations

import json
from pathlib import Path

from alice_eval import sampling


def _make_turn(
    ts: float,
    inbound: str = "hi",
    outbound: str = "hey",
    sender: str = "+14357091512",
) -> dict:
    return {
        "ts": ts,
        "sender_number": sender,
        "sender_name": "Jason",
        "inbound": inbound,
        "outbound": outbound,
        "error": None,
    }


def test_classify_image_via_inbound_marker():
    turn = _make_turn(1.0, inbound="check this screenshot", outbound="ok")
    sampling._annotate_turns([turn])
    assert sampling.classify_turn(turn) == "image"


def test_classify_tool_heavy_requires_two_distinct_markers():
    outbound = "ran `cozyhem status` and `gh pr list --repo foo/bar` for you"
    turn = _make_turn(1.0, inbound="status?", outbound=outbound)
    sampling._annotate_turns([turn])
    assert sampling.classify_turn(turn) == "tool-heavy"


def test_classify_design_requires_long_history():
    base_ts = 1_000_000.0
    turns = [
        _make_turn(base_ts + i, inbound="more", outbound="ok")
        for i in range(7)
    ]
    sampling._annotate_turns(turns)
    # Position > 5 in conversation → design.
    assert sampling.classify_turn(turns[-1]) == "design"


def test_classify_edge_marker_wins_over_tactical():
    turn = _make_turn(
        1.0, inbound="sorry, can you check?", outbound="sure"
    )
    sampling._annotate_turns([turn])
    assert sampling.classify_turn(turn) == "edge"


def test_classify_tactical_for_short_low_context_text():
    turn = _make_turn(1.0, inbound="lift weights for today?", outbound="bench 100")
    sampling._annotate_turns([turn])
    assert sampling.classify_turn(turn) == "tactical"


def test_classify_conversational_catchall():
    text = "x" * 300
    turn = _make_turn(1.0, inbound=text, outbound="ok")
    sampling._annotate_turns([turn])
    assert sampling.classify_turn(turn) == "conversational"


def test_group_into_conversations_respects_gap_and_sender():
    base = 1_000_000.0
    turns = [
        _make_turn(base + 0, sender="+1"),
        _make_turn(base + 10, sender="+1"),   # same conv
        _make_turn(base + 1000, sender="+1"),  # gap > 300s, new conv
        _make_turn(base + 1010, sender="+2"),  # different sender, new conv
    ]
    convs = sampling.group_into_conversations(turns)
    assert len(convs) == 3
    assert [len(c) for c in convs] == [2, 1, 1]


def test_stratified_sample_deterministic_with_seed():
    turns: list[dict] = []
    base = 1_000_000.0
    # 20 tactical-ish
    for i in range(20):
        turns.append(_make_turn(base + i, inbound=f"q{i}", outbound="ok"))
    # 10 design — long conversation
    for i in range(10):
        turns.append(_make_turn(base + 10_000 + i, inbound="more", outbound="ok"))
    sampling._annotate_turns(turns)

    targets = {"tactical": 5, "design": 3}
    a = sampling.stratified_sample(turns, targets, seed=42)
    b = sampling.stratified_sample(turns, targets, seed=42)
    a_ids = [t["turn_id"] for t in a]
    b_ids = [t["turn_id"] for t in b]
    assert a_ids == b_ids


def test_stratified_sample_shortfall_backfills_from_catchall():
    base = 1_000_000.0
    turns: list[dict] = []
    # 3 tactical (request 5 → 2-turn shortfall)
    for i in range(3):
        turns.append(_make_turn(base + i, inbound=f"q{i}", outbound="ok"))
    # 10 conversational (long inbound)
    for i in range(10):
        turns.append(
            _make_turn(base + 100 + i, inbound="x" * 300, outbound="ok")
        )
    sampling._annotate_turns(turns)
    targets = {"tactical": 5, "conversational": 0}
    sample = sampling.stratified_sample(turns, targets, seed=42)
    # 3 tactical taken; backfill from conversational catch-all fills
    # the remaining 2 slots so total hits the requested 5.
    assert len(sample) == 5


def test_main_sample_writes_jsonl(tmp_path: Path):
    base = 1_000_000.0
    rows = [
        _make_turn(base + i, inbound=f"hi {i}", outbound="hey")
        for i in range(40)
    ]
    # Inject a few design + tool-heavy + edge to balance categories.
    rows.append(
        _make_turn(base + 200, inbound="sorry can you check", outbound="ok")
    )
    rows.append(
        _make_turn(
            base + 201,
            inbound="status?",
            outbound="ran `cozyhem status` and `gh pr list --repo a/b`",
        )
    )

    log_path = tmp_path / "speaking-turns.jsonl"
    with log_path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    out_path = tmp_path / "eval_sample.jsonl"
    result = sampling.main_sample(
        log_path=log_path,
        out_path=out_path,
        seed=42,
        lookback_days=28,
        targets={"tactical": 5, "edge": 1, "tool-heavy": 1, "conversational": 1},
    )

    assert out_path.exists()
    written = [json.loads(line) for line in out_path.read_text().splitlines() if line]
    assert len(written) == len(result.sample)
    assert all("turn_id" in row and "sampled_category" in row for row in written)
