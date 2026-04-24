"""Tests for the send_message tool."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from alice_speaking.tools import messaging


def test_resolve_recipient_name_lookup(cfg):
    assert messaging._resolve_recipient("jason", cfg) == "+15555550100"
    assert messaging._resolve_recipient("Owner", cfg) == "+15555550100"
    assert messaging._resolve_recipient("KATIE", cfg) == "+15555550101"


def test_resolve_recipient_e164_passthrough(cfg):
    assert messaging._resolve_recipient("+15555550100", cfg) == "+15555550100"
    # Unknown E.164 is still trusted (daemon sends to whoever was asked).
    assert messaging._resolve_recipient("+19999999999", cfg) == "+19999999999"


def test_resolve_recipient_unknown_returns_none(cfg):
    assert messaging._resolve_recipient("bob", cfg) is None
    assert messaging._resolve_recipient("", cfg) is None


def test_build_rejects_missing_transport(cfg):
    with pytest.raises(ValueError):
        messaging.build(cfg)  # no signal, no sender


def test_send_message_happy_path(cfg):
    sent: list[tuple[str, str]] = []

    async def fake_sender(recipient: str, message: str) -> None:
        sent.append((recipient, message))

    tools = messaging.build(cfg, sender=fake_sender)
    assert len(tools) == 1
    send_tool = tools[0]
    assert send_tool.name == "send_message"

    result = asyncio.run(
        send_tool.handler({"recipient": "jason", "message": "hello"})
    )
    assert result.get("isError") is not True
    assert sent == [("+15555550100", "hello")]


def test_send_message_unknown_recipient(cfg):
    async def fake_sender(recipient: str, message: str) -> None:
        raise AssertionError("should not be called")

    tools = messaging.build(cfg, sender=fake_sender)
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler({"recipient": "alice", "message": "x"})
    )
    assert result["isError"] is True
    assert "could not resolve recipient" in result["content"][0]["text"]


def test_send_message_empty_message(cfg):
    async def fake_sender(recipient: str, message: str) -> None:
        raise AssertionError("should not be called")

    tools = messaging.build(cfg, sender=fake_sender)
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler({"recipient": "jason", "message": "   "})
    )
    assert result["isError"] is True
    assert "message must be a non-empty string" in result["content"][0]["text"]


def test_send_message_propagates_send_failure(cfg):
    async def flaky_sender(recipient: str, message: str) -> None:
        raise RuntimeError("signal offline")

    tools = messaging.build(cfg, sender=flaky_sender)
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler({"recipient": "jason", "message": "ping"})
    )
    assert result["isError"] is True
    assert "signal offline" in result["content"][0]["text"]


def test_send_message_via_signal_client(cfg):
    """When ``signal=`` is passed instead of ``sender=``, the tool should
    call SignalClient.send directly."""
    sent: list[tuple[str, str]] = []

    class FakeSignal:
        async def send(self, recipient: str, message: str) -> None:
            sent.append((recipient, message))

    tools = messaging.build(cfg, signal=FakeSignal())
    send_tool = tools[0]
    result = asyncio.run(
        send_tool.handler({"recipient": "katie", "message": "hi k"})
    )
    assert result.get("isError") is not True
    assert sent == [("+15555550101", "hi k")]
