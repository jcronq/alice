"""Tests for inbound message batching at the SignalTransport.

When messages arrive while Alice is mid-turn, they queue up on the
SignalTransport's per-transport inbox. On the next consumer iteration
the transport drains all currently-queued messages from the same
sender and processes them as a single batched turn — same UX as
Claude Code's input queue, just relocated from the daemon's shared
queue (Phase 2a of plan 01) so other transports can't get tangled
in Signal's batching.
"""

from __future__ import annotations

import asyncio

from alice_speaking.signal_client import SignalEnvelope
from alice_speaking.transports import (
    ChannelRef,
    InboundMessage,
    Principal,
)
from alice_speaking.transports.discord import DiscordEvent
from alice_speaking.transports.signal import SignalEvent, SignalTransport


def _transport() -> SignalTransport:
    """A SignalTransport with a stub client; the producer/consumer
    aren't started, so we can poke at the inbox directly."""
    return SignalTransport(signal_client=object())


def _sig(source: str, ts: int, body: str = "", name: str = "Owner") -> SignalEvent:
    return SignalEvent(
        envelope=SignalEnvelope(timestamp=ts, source=source, body=body),
        sender_name=name,
    )


def test_drain_batch_returns_only_head_when_inbox_empty():
    t = _transport()
    head = _sig("+15555550100", 1, "first")
    batch = t._drain_batch(head)
    assert len(batch) == 1
    assert batch[0] is head


def test_drain_batch_collects_same_sender():
    t = _transport()
    head = _sig("+15555550100", 1, "first")
    t._inbox.put_nowait(_sig("+15555550100", 2, "second"))
    t._inbox.put_nowait(_sig("+15555550100", 3, "third"))
    batch = t._drain_batch(head)
    bodies = [ev.envelope.body for ev in batch]
    assert bodies == ["first", "second", "third"]
    assert t._inbox.empty()


def test_drain_batch_preserves_other_sender():
    t = _transport()
    head = _sig("+15555550100", 1, "from owner")
    t._inbox.put_nowait(_sig("+15555550100", 2, "also owner"))
    t._inbox.put_nowait(_sig("+15555550101", 3, "from friend", name="Friend"))
    t._inbox.put_nowait(_sig("+15555550100", 4, "more owner"))
    batch = t._drain_batch(head)

    # Owner's three messages batch together; Friend's stays in the inbox.
    assert [ev.envelope.body for ev in batch] == [
        "from owner",
        "also owner",
        "more owner",
    ]
    assert t._inbox.qsize() == 1
    held = t._inbox.get_nowait()
    assert held.envelope.body == "from friend"
    assert held.envelope.source == "+15555550101"


def _discord_event(text: str, msg_id: str = "m") -> DiscordEvent:
    """Hand-build a DiscordEvent without touching the discord client."""
    principal = Principal(
        transport="discord", native_id="user:1234", display_name="Friend"
    )
    origin = ChannelRef(transport="discord", address="user:1234", durable=True)
    return DiscordEvent(
        message=InboundMessage(
            principal=principal,
            origin=origin,
            text=text,
            timestamp=0.0,
            metadata={"discord_message_id": msg_id},
        )
    )


def test_burst_does_not_disturb_other_transports():
    """Per-transport queue isolation (Phase 2a of plan 01).

    A burst of Signal events interleaved with Discord events must NOT
    cause Signal's batch coalescing to reach into the daemon's main
    queue. The exit criterion: after Signal drains its batch, every
    Discord event remains on the main queue in its original order.
    """
    sig = _transport()
    main_queue: asyncio.Queue = asyncio.Queue()

    # Simulate the producers landing events on their respective queues.
    # Real interleaving: signal, discord, signal, discord, signal, signal.
    sig._inbox.put_nowait(_sig("+15555550100", 1, "sig-1"))
    main_queue.put_nowait(_discord_event("disc-A", "A"))
    sig._inbox.put_nowait(_sig("+15555550100", 2, "sig-2"))
    main_queue.put_nowait(_discord_event("disc-B", "B"))
    sig._inbox.put_nowait(_sig("+15555550100", 3, "sig-3"))
    sig._inbox.put_nowait(_sig("+15555550100", 4, "sig-4"))

    # Signal's consumer loop pulls head + drains the rest.
    head = sig._inbox.get_nowait()
    batch = sig._drain_batch(head)

    # All four signal events coalesce into one batch in arrival order.
    assert [ev.envelope.body for ev in batch] == [
        "sig-1",
        "sig-2",
        "sig-3",
        "sig-4",
    ]
    assert sig._inbox.empty()

    # Discord events untouched — same order, same count.
    drained: list[DiscordEvent] = []
    while not main_queue.empty():
        drained.append(main_queue.get_nowait())
    assert [ev.message.text for ev in drained] == ["disc-A", "disc-B"]
