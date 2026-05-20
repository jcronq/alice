"""Replay harness unit tests — fully mocked HTTP.

Both candidate providers go through :class:`httpx.AsyncClient`. We
swap in a :class:`httpx.MockTransport` that returns canned JSON for
the Anthropic and OpenAI-compatible URLs, then assert the resulting
:class:`ReplayResult` rows carry the right shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from eval import replay
from eval.replay import Candidate, ReplayResult


@pytest.fixture
def synthetic_turn():
    return {
        "ts": 1_779_131_000.0,
        "sender_number": "+14357091512",
        "sender_name": "Jason",
        "inbound": "ping me at +14357091512 please",
        "outbound": "ok",
        "error": None,
        "turn_id": "turn_1779131000000",
        "sampled_category": "tactical",
        "history_pos": 1,
    }


@pytest.fixture
def speaking_log(tmp_path: Path, synthetic_turn):
    log_path = tmp_path / "speaking-turns.jsonl"
    with log_path.open("w") as fh:
        fh.write(json.dumps(synthetic_turn) + "\n")
    return log_path


def _mock_transport():
    """Return an httpx MockTransport that distinguishes the two
    candidate endpoints by URL path."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            body = json.loads(request.content.decode())
            assert body["model"].startswith("claude-")
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "opus says hi"}],
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                },
            )
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "qwen says hi"}}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def candidates():
    return [
        Candidate(
            id="opus",
            label="Opus",
            provider="anthropic",
            model="claude-opus-4-7-20260515",
            base_url="https://api.anthropic.com",
            auth_env=["ANTHROPIC_API_KEY"],
        ),
        Candidate(
            id="qwen",
            label="Qwen",
            provider="openai_compatible",
            model="Qwen3.6-27B-Q4_K_M",
            base_url="http://10.20.30.147:8033/v1",
            auth_env=[],
        ),
    ]


def test_build_messages_redacts_phone(synthetic_turn):
    messages = replay.build_messages(synthetic_turn, [synthetic_turn])
    assert "+14357091512" not in messages[-1]["content"]
    assert "[REDACTED_PHONE]" in messages[-1]["content"]


def test_build_messages_includes_prior_turns(synthetic_turn):
    prior = {
        "ts": synthetic_turn["ts"] - 30,
        "sender_number": synthetic_turn["sender_number"],
        "inbound": "hi",
        "outbound": "hey",
        "turn_id": "turn_prior",
    }
    messages = replay.build_messages(synthetic_turn, [prior, synthetic_turn])
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hi"
    assert messages[1]["role"] == "assistant"
    assert messages[-1]["content"].startswith("ping me at")


def test_replay_turn_anthropic_records_tokens(
    synthetic_turn, candidates, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def runner():
        transport = _mock_transport()
        async with httpx.AsyncClient(transport=transport) as client:
            return await replay.replay_turn(
                candidates[0],
                synthetic_turn,
                [synthetic_turn],
                system_prompt="you are Alice",
                client=client,
            )

    result = asyncio.run(runner())
    assert isinstance(result, ReplayResult)
    assert result.status == "ok"
    assert result.candidate_id == "opus"
    assert result.output == "opus says hi"
    assert result.input_tokens == 12
    assert result.output_tokens == 4
    assert result.latency_ms >= 0


def test_replay_turn_openai_records_tokens(synthetic_turn, candidates):
    async def runner():
        transport = _mock_transport()
        async with httpx.AsyncClient(transport=transport) as client:
            return await replay.replay_turn(
                candidates[1],
                synthetic_turn,
                [synthetic_turn],
                system_prompt="you are Alice",
                client=client,
            )

    result = asyncio.run(runner())
    assert result.status == "ok"
    assert result.candidate_id == "qwen"
    assert result.output == "qwen says hi"
    assert result.input_tokens == 11
    assert result.output_tokens == 3


def test_replay_turn_records_error_on_failure(synthetic_turn, candidates):
    """A 500 from the provider becomes status=error rather than
    raising — the gather() in main_replay must keep going."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async def runner():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            return await replay.replay_turn(
                candidates[1],
                synthetic_turn,
                [synthetic_turn],
                system_prompt="you are Alice",
                client=client,
            )

    result = asyncio.run(runner())
    assert result.status == "error"
    assert result.error is not None
    assert "500" in result.error or "HTTPStatusError" in result.error


def test_run_all_writes_jsonl_per_candidate(
    tmp_path, synthetic_turn, candidates, speaking_log, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    out_dir = tmp_path / "outputs"

    async def runner():
        async with httpx.AsyncClient(
            transport=_mock_transport()
        ) as client:
            await replay._run_all(
                candidates,
                [synthetic_turn],
                log_path=speaking_log,
                out_dir=out_dir,
                concurrency=2,
                system_prompt="you are Alice",
                client=client,
            )

    asyncio.run(runner())

    opus_rows = (out_dir / "eval_outputs_opus.jsonl").read_text().splitlines()
    qwen_rows = (out_dir / "eval_outputs_qwen.jsonl").read_text().splitlines()
    assert len(opus_rows) == 1
    assert len(qwen_rows) == 1
    parsed = json.loads(opus_rows[0])
    assert set(parsed.keys()) >= {
        "turn_id", "candidate_id", "category", "output",
        "latency_ms", "input_tokens", "output_tokens",
        "status", "error", "request_ts",
    }


def test_load_candidates_parses_config(tmp_path: Path):
    cfg = tmp_path / "candidates.json"
    cfg.write_text(json.dumps({
        "candidates": [
            {"id": "x", "label": "X", "provider": "anthropic",
             "model": "m", "auth_env": ["A"]},
        ]
    }))
    cands = replay.load_candidates(cfg)
    assert len(cands) == 1 and cands[0].id == "x"
