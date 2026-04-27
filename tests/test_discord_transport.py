"""Tests for the Discord transport scaffolding.

Deep transport behavior (login handshake, on_message conversion, outbound
DM delivery) needs a real Discord client and is exercised live, not in
this suite. These tests cover the bits we can touch without a network:
construction, capability advertisement, and outbound rendering through a
stubbed client.
"""

from __future__ import annotations

import asyncio

import pytest

from alice_speaking.transports import DiscordTransport
from alice_speaking.transports.base import (
    DISCORD_CAPS,
    ChannelRef,
    OutboundMessage,
)
from alice_speaking.transports.discord import _parse_address


def test_construction_requires_token():
    with pytest.raises(ValueError):
        DiscordTransport(token="")


def test_caps_and_name_match_DISCORD_CAPS():
    t = DiscordTransport(token="xxx")
    assert t.name == "discord"
    assert t.caps is DISCORD_CAPS


def test_send_before_start_raises():
    t = DiscordTransport(token="xxx")

    async def go():
        await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="discord", address="123", durable=True
                ),
                text="hi",
            )
        )

    with pytest.raises(RuntimeError, match="before start"):
        asyncio.run(go())


def test_send_renders_and_chunks(monkeypatch):
    """Outbound goes through render() (limited-markdown stripping +
    chunking) then per-chunk user.send."""
    t = DiscordTransport(token="xxx")

    sent: list[str] = []

    class _StubUser:
        async def send(self, payload: str) -> None:
            sent.append(payload)

    user = _StubUser()
    t._user_cache["123"] = user
    # The send code path checks self._client is not None as a sanity
    # gate before resolving users; satisfy it with any truthy stand-in.
    t._client = object()

    async def go():
        await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="discord", address="123", durable=True
                ),
                text="**hello** _world_",
            )
        )

    asyncio.run(go())
    # One chunk under DISCORD_CAPS.max_message_bytes; limited-markdown
    # leaves bold/italics intact.
    assert len(sent) == 1
    assert "**hello**" in sent[0]
    assert "_world_" in sent[0]


def test_parse_address_handles_user_and_channel_prefixes():
    assert _parse_address("user:123") == ("user", 123)
    assert _parse_address("channel:456") == ("channel", 456)
    # Bare numeric id is back-compat-treated as user.
    assert _parse_address("789") == ("user", 789)


def test_parse_address_rejects_unknown_kind():
    with pytest.raises(ValueError):
        _parse_address("guild:123")


def test_parse_address_rejects_non_numeric_id():
    with pytest.raises(ValueError):
        _parse_address("user:abc")


def test_send_routes_channel_address_to_channel_send():
    """Address ``channel:<id>`` → fetch_channel + channel.send (not DM)."""
    t = DiscordTransport(token="xxx")
    sent_via: list[str] = []

    class _StubChannel:
        async def send(self, payload: str) -> None:
            sent_via.append(payload)

    class _StubClient:
        def get_channel(self, _id: int):
            return _StubChannel()

    t._client = _StubClient()

    async def go():
        await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="discord", address="channel:42", durable=True
                ),
                text="hello channel",
            )
        )

    asyncio.run(go())
    assert sent_via == ["hello channel"]


def test_send_attachments_logged_and_dropped(monkeypatch, caplog):
    """Discord attachments aren't implemented yet — accepted, logged,
    dropped (text still goes through)."""
    t = DiscordTransport(token="xxx")

    sent: list[str] = []

    class _StubUser:
        async def send(self, payload: str) -> None:
            sent.append(payload)

    t._user_cache["123"] = _StubUser()
    t._client = object()

    async def go():
        await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="discord", address="123", durable=True
                ),
                text="hello",
                attachments=["/tmp/x.png"],
            )
        )

    with caplog.at_level("WARNING"):
        asyncio.run(go())
    assert sent == ["hello"]
    assert any("attachment" in r.message for r in caplog.records)
