"""Shared pytest fixtures for the alice-speaking test suite."""

from __future__ import annotations

import pathlib

import pytest

from alice_speaking.infra.config import Config, SPEAKING_DEFAULTS
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)


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
        work_dir=mind_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=state_dir / "signal.log",
        offset_path=state_dir / "offset",
        seen_path=state_dir / "seen",
        turn_log_path=mind_dir / "inner" / "state" / "speaking-turns.jsonl",
        event_log_path=state_dir / "speaking.log",
        principals_path=mind_dir / "config" / "principals.yaml",
        allowed_senders_fallback={
            "+15555550100": "Owner",
            "+15555550101": "Friend",
        },
        speaking=dict(SPEAKING_DEFAULTS),
    )


@pytest.fixture
def address_book() -> AddressBook:
    """Standard two-principal address book matching the legacy
    ALLOWED_SENDERS fixture: Owner on signal+cli, Friend on signal only."""
    return AddressBook([
        PrincipalRecord(
            id="owner",
            display_name="Owner",
            channels=[
                PrincipalChannel(
                    transport="signal",
                    address="+15555550100",
                    durable=True,
                    preferred=True,
                ),
                PrincipalChannel(
                    transport="cli", address="1000", durable=False
                ),
            ],
        ),
        PrincipalRecord(
            id="friend",
            display_name="Friend",
            channels=[
                PrincipalChannel(
                    transport="signal",
                    address="+15555550101",
                    durable=True,
                    preferred=True,
                ),
            ],
        ),
    ])
