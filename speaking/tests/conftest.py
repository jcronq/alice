"""Shared pytest fixtures for the alice-speaking test suite."""

from __future__ import annotations

import pathlib

import pytest

from alice_speaking.config import AllowedSender, Config, SPEAKING_DEFAULTS


@pytest.fixture
def cfg(tmp_path: pathlib.Path) -> Config:
    """Minimal Config for tests. All paths live under tmp_path so tests
    can't stomp on real state."""
    mind_dir = tmp_path / "mind"
    state_dir = tmp_path / "state"
    mind_dir.mkdir()
    state_dir.mkdir()
    return Config(
        signal_api="http://127.0.0.1:8080",
        signal_account="+15550000000",
        oauth_token="dummy",
        allowed_senders={
            "+15555550100": AllowedSender(number="+15555550100", name="Owner"),
            "+15555550101": AllowedSender(number="+15555550101", name="Friend"),
        },
        work_dir=mind_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=state_dir / "signal.log",
        offset_path=state_dir / "offset",
        seen_path=state_dir / "seen",
        turn_log_path=mind_dir / "inner" / "state" / "speaking-turns.jsonl",
        event_log_path=state_dir / "speaking.log",
        speaking=dict(SPEAKING_DEFAULTS),
    )
