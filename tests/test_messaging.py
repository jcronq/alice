"""Tests for the send_message tool."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any, Optional

import pytest

from alice_speaking.tools import messaging
from alice_speaking.transports import ChannelRef


@pytest.fixture(autouse=True)
def _reset_send_dedup():
    """The idempotency guard keeps module-level state across calls; clear it
    between tests so dedup hits don't leak from one test into the next."""
    messaging._recent_sends.clear()
    yield
    messaging._recent_sends.clear()


# ---------------------------------------------------------------------------
# Recipient resolution


def test_resolve_recipient_name_lookup(address_book):
    owner = ChannelRef(transport="signal", address="+15555550100", durable=True)
    friend = ChannelRef(transport="signal", address="+15555550101", durable=True)
    assert messaging._resolve_recipient("owner", address_book) == owner
    # case-insensitive on display name + id
    assert messaging._resolve_recipient("Owner", address_book) == owner
    assert messaging._resolve_recipient("FRIEND", address_book) == friend


def test_resolve_recipient_e164_passthrough(address_book):
    expected = ChannelRef(transport="signal", address="+15555550100", durable=True)
    assert messaging._resolve_recipient("+15555550100", address_book) == expected
    # Unknown E.164 still trusts the caller — daemon sends to whoever was asked.
    assert messaging._resolve_recipient("+19999999999", address_book) == ChannelRef(
        transport="signal", address="+19999999999", durable=True
    )


def test_resolve_recipient_self_alias(address_book):
    assert (
        messaging._resolve_recipient("self", address_book) == messaging.SELF_RECIPIENT
    )
    assert (
        messaging._resolve_recipient("REPLY", address_book) == messaging.SELF_RECIPIENT
    )


def test_resolve_recipient_unknown_returns_none(address_book):
    assert messaging._resolve_recipient("bob", address_book) is None
    assert messaging._resolve_recipient("", address_book) is None


def test_build_rejects_missing_transport(cfg, address_book):
    with pytest.raises(ValueError):
        messaging.build(cfg, address_book=address_book)  # no signal, no sender


# ---------------------------------------------------------------------------
# send_message handler


def test_send_message_happy_path(cfg, address_book, tmp_path):
    sent: list[tuple[Any, str, Optional[list[str]]]] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append((recipient, message, attachments))

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    assert len(tools) == 1
    send_tool = tools[0]
    assert send_tool.name == "send_message"

    result = asyncio.run(send_tool.handler({"recipient": "owner", "message": "hello"}))
    assert result.get("isError") is not True
    assert sent == [
        (
            ChannelRef(transport="signal", address="+15555550100", durable=True),
            "hello",
            None,
        )
    ]


def test_send_message_unknown_recipient(cfg, address_book, tmp_path):
    async def fake_sender(*_, **__) -> None:
        raise AssertionError("should not be called")

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(send_tool.handler({"recipient": "alice", "message": "x"}))
    assert result["isError"] is True
    assert "could not resolve recipient" in result["content"][0]["text"]


def test_send_message_empty_message(cfg, address_book, tmp_path):
    async def fake_sender(*_, **__) -> None:
        raise AssertionError("should not be called")

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(send_tool.handler({"recipient": "owner", "message": "   "}))
    assert result["isError"] is True
    assert "message must be a non-empty string" in result["content"][0]["text"]


def test_send_message_propagates_send_failure(cfg, address_book, tmp_path):
    async def flaky_sender(*_, **__) -> None:
        raise RuntimeError("signal offline")

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=flaky_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(send_tool.handler({"recipient": "owner", "message": "ping"}))
    assert result["isError"] is True
    assert "signal offline" in result["content"][0]["text"]


def test_send_message_via_signal_client(cfg, address_book):
    """When ``signal=`` is passed instead of ``sender=``, the tool calls
    SignalClient.send directly. The shim unpacks ChannelRef → phone."""
    sent: list[tuple[str, str, Optional[list[str]]]] = []

    class FakeSignal:
        async def send(
            self,
            recipient: str,
            message: str,
            attachments: Optional[list[str]] = None,
        ) -> None:
            sent.append((recipient, message, attachments))

    tools = messaging.build(cfg, address_book=address_book, signal=FakeSignal())
    send_tool = tools[0]
    result = asyncio.run(send_tool.handler({"recipient": "friend", "message": "hi k"}))
    assert result.get("isError") is not True
    assert sent == [("+15555550101", "hi k", None)]


# ---------------------------------------------------------------------------
# Attachment support


def test_send_message_with_attachment_passes_path(cfg, address_book, tmp_path):
    """A valid file path should be staged into the outbox and forwarded as
    an absolute path to the underlying sender."""
    sent: list[tuple[Any, str, Optional[list[str]]]] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append((recipient, message, attachments))

    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    outbox = tmp_path / "outbox"
    tools = messaging.build(
        cfg, address_book=address_book, sender=fake_sender, outbox_dir=outbox
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler(
            {
                "recipient": "owner",
                "message": "look at this",
                "attachments": [str(src)],
            }
        )
    )
    assert result.get("isError") is not True, result
    assert "+1 attachment" in result["content"][0]["text"]
    assert len(sent) == 1
    recipient, message, attachments = sent[0]
    assert recipient == ChannelRef(
        transport="signal", address="+15555550100", durable=True
    )
    assert message == "look at this"
    assert attachments is not None
    assert len(attachments) == 1
    # Path was staged into the outbox, not passed through verbatim.
    staged = pathlib.Path(attachments[0])
    assert staged.parent == outbox
    assert staged.name.endswith("-shot.png")
    # And cleaned up after send.
    assert not staged.exists()


def test_send_message_no_attachments_field(cfg, address_book, tmp_path):
    """When the field is absent, the sender must receive None — not an
    empty list — so downstream code can keep its 'no media' fast path."""
    sent: list[tuple[Any, str, Optional[list[str]]]] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append((recipient, message, attachments))

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler({"recipient": "owner", "message": "no media"})
    )
    assert result.get("isError") is not True
    assert sent == [
        (
            ChannelRef(transport="signal", address="+15555550100", durable=True),
            "no media",
            None,
        )
    ]


def test_send_message_empty_attachments_treated_as_none(cfg, address_book, tmp_path):
    """Empty list is equivalent to no attachments — sender sees None and
    the outbox dir is not even touched."""
    sent: list[tuple[Any, str, Optional[list[str]]]] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append((recipient, message, attachments))

    outbox = tmp_path / "outbox"
    tools = messaging.build(
        cfg, address_book=address_book, sender=fake_sender, outbox_dir=outbox
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler(
            {"recipient": "owner", "message": "still nothing", "attachments": []}
        )
    )
    assert result.get("isError") is not True
    assert sent == [
        (
            ChannelRef(transport="signal", address="+15555550100", durable=True),
            "still nothing",
            None,
        )
    ]
    assert not outbox.exists()


def test_send_message_non_list_attachments_errors(cfg, address_book, tmp_path):
    """A scalar (or any non-list) attachments value is a tool-input
    error — sender must not be invoked."""

    async def fake_sender(*_, **__) -> None:
        raise AssertionError("should not be called")

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler(
            {
                "recipient": "owner",
                "message": "hi",
                "attachments": "/path/to/file.png",
            }
        )
    )
    assert result["isError"] is True
    assert "list of filesystem path strings" in result["content"][0]["text"]


def test_send_message_attachments_with_non_string_entry_errors(
    cfg, address_book, tmp_path
):
    """A list with a non-string entry is also rejected."""

    async def fake_sender(*_, **__) -> None:
        raise AssertionError("should not be called")

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler(
            {
                "recipient": "owner",
                "message": "hi",
                "attachments": ["/ok.png", 42],
            }
        )
    )
    assert result["isError"] is True
    assert "list of filesystem path strings" in result["content"][0]["text"]


def test_send_message_missing_attachment_path_errors(cfg, address_book, tmp_path):
    """A path that doesn't exist must surface a tool error before any
    send is attempted."""
    sent: list[Any] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append((recipient, message, attachments))

    tools = messaging.build(
        cfg,
        address_book=address_book,
        sender=fake_sender,
        outbox_dir=tmp_path / "outbox",
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler(
            {
                "recipient": "owner",
                "message": "hi",
                "attachments": [str(tmp_path / "nope.png")],
            }
        )
    )
    assert result["isError"] is True
    assert "FileNotFoundError" in result["content"][0]["text"]
    assert sent == []


def test_send_message_send_failure_cleans_up_staged_files(cfg, address_book, tmp_path):
    """If the underlying send raises, the staged copies still get
    swept — an exception during signal-cli upload shouldn't leak files
    into the spool dir."""

    async def flaky_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        # Confirm the file was staged before we fail.
        assert attachments and pathlib.Path(attachments[0]).exists()
        raise RuntimeError("signal offline")

    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.7 fake")
    outbox = tmp_path / "outbox"

    tools = messaging.build(
        cfg, address_book=address_book, sender=flaky_sender, outbox_dir=outbox
    )
    send_tool = tools[0]

    result = asyncio.run(
        send_tool.handler(
            {
                "recipient": "owner",
                "message": "boom",
                "attachments": [str(src)],
            }
        )
    )
    assert result["isError"] is True
    # No leftover copies — everything we staged was cleaned up.
    assert outbox.exists()
    assert list(outbox.iterdir()) == []


# ---------------------------------------------------------------------------
# Idempotency guard (task-0496)


def test_send_message_deduplicates_identical_resend(cfg, address_book, tmp_path):
    """A second identical send to the same recipient inside the TTL window
    is suppressed — the underlying sender fires exactly once."""
    sent: list[tuple[Any, str, Optional[list[str]]]] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append((recipient, message, attachments))

    tools = messaging.build(
        cfg, address_book=address_book, sender=fake_sender, outbox_dir=tmp_path / "o"
    )
    send_tool = tools[0]

    first = asyncio.run(send_tool.handler({"recipient": "owner", "message": "ping"}))
    second = asyncio.run(send_tool.handler({"recipient": "owner", "message": "ping"}))

    assert first.get("isError") is not True
    assert second.get("isError") is not True
    assert "duplicate suppressed" in second["content"][0]["text"]
    assert len(sent) == 1  # the resend never reached the sender


def test_send_message_distinct_messages_not_deduplicated(cfg, address_book, tmp_path):
    sent: list[str] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append(message)

    tools = messaging.build(
        cfg, address_book=address_book, sender=fake_sender, outbox_dir=tmp_path / "o"
    )
    send_tool = tools[0]

    asyncio.run(send_tool.handler({"recipient": "owner", "message": "one"}))
    asyncio.run(send_tool.handler({"recipient": "owner", "message": "two"}))

    assert sent == ["one", "two"]


def test_send_message_failed_send_allows_retry(cfg, address_book, tmp_path):
    """A send that raises must NOT poison the dedup cache — the user's retry
    of the same message has to go through."""
    calls = {"n": 0}

    async def flaky_then_ok(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("signal offline")

    tools = messaging.build(
        cfg, address_book=address_book, sender=flaky_then_ok, outbox_dir=tmp_path / "o"
    )
    send_tool = tools[0]

    first = asyncio.run(send_tool.handler({"recipient": "owner", "message": "retry me"}))
    second = asyncio.run(
        send_tool.handler({"recipient": "owner", "message": "retry me"})
    )

    assert first["isError"] is True
    assert second.get("isError") is not True
    assert "duplicate suppressed" not in second["content"][0]["text"]
    assert calls["n"] == 2  # retry reached the sender, was not deduped


def test_send_message_dedup_expires_after_ttl(
    cfg, address_book, tmp_path, monkeypatch
):
    """Once the TTL window passes, the same message sends again."""
    sent: list[str] = []

    async def fake_sender(
        recipient: messaging.ResolvedRecipient,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        sent.append(message)

    clock = {"t": 1000.0}
    monkeypatch.setattr(messaging.time, "monotonic", lambda: clock["t"])

    tools = messaging.build(
        cfg, address_book=address_book, sender=fake_sender, outbox_dir=tmp_path / "o"
    )
    send_tool = tools[0]

    asyncio.run(send_tool.handler({"recipient": "owner", "message": "tick"}))
    clock["t"] += messaging._SEND_DEDUP_TTL_S + 1
    asyncio.run(send_tool.handler({"recipient": "owner", "message": "tick"}))

    assert sent == ["tick", "tick"]
