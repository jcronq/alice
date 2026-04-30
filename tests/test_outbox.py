"""Phase 6a of plan 01: OutboxRouter unit tests.

Three contracts:

1. Routing — :meth:`dispatch` looks up the right transport for the
   given ``ChannelRef.transport`` and calls its ``send``. Unknown
   transport raises with a clear message.
2. Quiet-hours queue — Signal / Discord deliveries land on the
   :class:`QuietQueue` when ``is_quiet_hours`` returns True, unless
   ``bypass_quiet`` is set. CLI / A2A never queue regardless.
3. Send-event emission — every successful delivery emits one
   ``<transport>_send`` event with the canonical field shape;
   queued deliveries emit ``quiet_queue_enter`` instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest

from alice_speaking import outbox as outbox_module
from alice_speaking.events import EventLogger
from alice_speaking.outbox import OutboxRouter
from alice_speaking.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)
from alice_speaking.quiet_hours import QuietQueue
from alice_speaking.transports import ChannelRef


# ---------------------------------------------------------------------------
# Test fakes


class _StubTransport:
    """Records every send. ``send`` returns the stub-configured
    chunk count so the router emits the right ``chunk_count``."""

    name = "stub"

    def __init__(self, chunk_count: int = 1) -> None:
        self._chunks = chunk_count
        self.sent: list = []

    async def send(self, out) -> int:
        self.sent.append(out)
        return self._chunks


# ---------------------------------------------------------------------------
# Helpers


def _read_events(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _make_router(
    tmp_path,
    *,
    transports: dict[str, Optional[object]],
    speaking_cfg: Optional[dict] = None,
) -> tuple[OutboxRouter, AddressBook, QuietQueue, EventLogger, "outbox_module.Path"]:
    address_book = AddressBook(
        [
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
                ],
            ),
        ]
    )
    quiet_queue = QuietQueue(tmp_path / "quiet-queue.jsonl")
    log_path = tmp_path / "speaking.log"
    events = EventLogger(log_path)
    router = OutboxRouter(
        transport_for=lambda name: transports.get(name),
        address_book=address_book,
        events=events,
        quiet_queue=quiet_queue,
        speaking_cfg=speaking_cfg or {},
    )
    return router, address_book, quiet_queue, events, log_path


# ---------------------------------------------------------------------------
# Routing


def test_dispatch_routes_to_named_transport(tmp_path):
    cli = _StubTransport(chunk_count=2)
    router, _, _, _, log_path = _make_router(tmp_path, transports={"cli": cli})

    channel = ChannelRef(transport="cli", address="conn-1", durable=False)
    asyncio.run(router.dispatch(channel, "hi"))

    assert len(cli.sent) == 1
    assert cli.sent[0].destination == channel
    assert cli.sent[0].text == "hi"
    events = _read_events(log_path)
    assert any(ev["event"] == "cli_send" and ev["chunk_count"] == 2 for ev in events)


def test_dispatch_unknown_transport_raises(tmp_path):
    router, _, _, _, _ = _make_router(tmp_path, transports={})
    channel = ChannelRef(transport="nope", address="x", durable=True)
    with pytest.raises(RuntimeError, match="not available"):
        asyncio.run(router.dispatch(channel, "hi"))


# ---------------------------------------------------------------------------
# Quiet hours


def test_signal_send_queues_in_quiet_hours(tmp_path, monkeypatch):
    sig = _StubTransport()
    router, _, queue, _, log_path = _make_router(
        tmp_path, transports={"signal": sig}
    )
    monkeypatch.setattr(outbox_module, "is_quiet_hours", lambda *_a, **_k: True)

    channel = ChannelRef(
        transport="signal", address="+15555550100", durable=True
    )
    asyncio.run(router.dispatch(channel, "good morning"))

    # Sent-via-transport never happened — went to queue.
    assert sig.sent == []
    assert queue.size() == 1
    drained = queue.drain()[0]
    assert drained.transport == "signal"
    assert drained.recipient == "+15555550100"
    assert drained.text == "good morning"

    events = _read_events(log_path)
    assert any(ev["event"] == "quiet_queue_enter" for ev in events)


def test_signal_send_bypasses_quiet_when_flagged(tmp_path, monkeypatch):
    sig = _StubTransport()
    router, _, queue, _, log_path = _make_router(
        tmp_path, transports={"signal": sig}
    )
    monkeypatch.setattr(outbox_module, "is_quiet_hours", lambda *_a, **_k: True)

    channel = ChannelRef(
        transport="signal", address="+15555550100", durable=True
    )
    asyncio.run(
        router.dispatch(channel, "wake up", bypass_quiet=True, emergency=True)
    )

    assert len(sig.sent) == 1
    assert queue.size() == 0
    events = _read_events(log_path)
    [signal_send] = [ev for ev in events if ev["event"] == "signal_send"]
    assert signal_send["emergency"] is True
    assert signal_send["bypassed_quiet"] is True


def test_cli_send_never_queues(tmp_path, monkeypatch):
    """Interactive transports (CLI, A2A) ignore quiet hours entirely."""
    cli = _StubTransport()
    router, _, queue, _, _ = _make_router(tmp_path, transports={"cli": cli})
    monkeypatch.setattr(outbox_module, "is_quiet_hours", lambda *_a, **_k: True)

    channel = ChannelRef(transport="cli", address="conn-1", durable=False)
    asyncio.run(router.dispatch(channel, "hello"))

    assert len(cli.sent) == 1
    assert queue.size() == 0


# ---------------------------------------------------------------------------
# Attachment policy


def test_attachments_stripped_for_non_signal_transports(tmp_path):
    discord = _StubTransport()
    router, _, _, _, _ = _make_router(
        tmp_path, transports={"discord": discord}
    )
    channel = ChannelRef(transport="discord", address="user:123", durable=True)
    asyncio.run(
        router.dispatch(channel, "hi", attachments=["/tmp/a.png"])
    )
    [out] = discord.sent
    assert out.attachments == []


def test_attachments_passed_through_for_signal(tmp_path):
    sig = _StubTransport()
    router, _, _, _, _ = _make_router(tmp_path, transports={"signal": sig})
    channel = ChannelRef(
        transport="signal", address="+15555550100", durable=True
    )
    asyncio.run(
        router.dispatch(channel, "look", attachments=["/tmp/a.png"])
    )
    [out] = sig.sent
    assert out.attachments == ["/tmp/a.png"]


# ---------------------------------------------------------------------------
# Principal-display-name fallback


def test_send_event_uses_principal_display_name_when_address_unresolvable(
    tmp_path,
):
    """CLI's channel.address is an ephemeral conn_id with no
    address-book entry. The router must fall back to the in-flight
    turn's principal display name when the address-book lookup
    returns the address verbatim."""
    cli = _StubTransport()
    router, _, _, _, log_path = _make_router(tmp_path, transports={"cli": cli})

    channel = ChannelRef(transport="cli", address="conn-xyz", durable=False)
    asyncio.run(
        router.dispatch(channel, "hi", principal_display_name="Owner")
    )
    events = _read_events(log_path)
    [cli_send] = [ev for ev in events if ev["event"] == "cli_send"]
    assert cli_send["sender_name"] == "Owner"
