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

from alice_speaking.infra.signal_rpc import SignalEnvelope
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


# ----------------------------------------------------------------------
# Transport drain (graceful shutdown). Separate from _drain_batch above:
# this is the lifecycle hook the daemon calls on first SIGTERM so a
# blue/green deploy can release the lease without killing the in-flight
# Signal turn.

class _FakeRPC:
    """Minimal SignalRPC stand-in that yields a controllable stream."""

    def __init__(self, envelopes: list[SignalEnvelope]) -> None:
        self._envelopes = envelopes
        self._gate = asyncio.Event()  # holds receive() open after the list

    async def receive(self):
        for env in self._envelopes:
            yield env
        # Block forever (until cancelled) so receive() looks like a
        # real long-lived stream — drain has to cancel us.
        await self._gate.wait()


class _StubAddressBook:
    def is_allowed(self, *_args, **_kwargs) -> bool:
        return True

    def display_name_for(self, *_args, **_kwargs) -> str:
        return "Owner"


class _StubDedup:
    def __init__(self) -> None:
        self._seen: set[int] = set()

    def seen(self, ts: int) -> bool:
        return ts in self._seen

    def mark(self, ts: int) -> None:
        self._seen.add(ts)


class _StubCtx:
    def __init__(self) -> None:
        self.address_book = _StubAddressBook()
        self.dedup = _StubDedup()


def test_drain_stops_produce_and_waits_for_inbox_to_empty():
    """drain() must:
    1. Cancel the inner _produce task so signal-cli polling stops.
    2. Block until _consume has finished every event already pulled
       (i.e., _inbox.join() returns).
    """

    async def _exercise() -> None:
        envelopes = [
            SignalEnvelope(timestamp=t, source="+15555550100", body=f"msg-{t}")
            for t in (1, 2, 3)
        ]
        t = SignalTransport(signal_client=_FakeRPC(envelopes))

        consumed: list[SignalEvent] = []

        async def _fake_consume(_ctx) -> None:
            while True:
                ev = await t._inbox.get()
                try:
                    # Simulate per-turn work that takes a real moment so
                    # drain() has something to wait on rather than
                    # racing past an empty inbox.
                    await asyncio.sleep(0.02)
                    consumed.append(ev)
                finally:
                    t._inbox.task_done()

        t._consume = _fake_consume  # type: ignore[assignment]

        ctx = _StubCtx()
        run_task = t.producer(ctx)
        assert run_task is not None
        try:
            # Let _produce push all envelopes into the inbox.
            for _ in range(50):
                if t._inbox.qsize() == 3 or len(consumed) > 0:
                    break
                await asyncio.sleep(0.01)

            await t.drain()

            # All three envelopes ran through the consumer.
            assert len(consumed) == 3
            assert [ev.envelope.timestamp for ev in consumed] == [1, 2, 3]
            # Producer is gone; inbox is empty.
            assert t._produce_task is not None and t._produce_task.done()
            assert t._inbox.empty()
        finally:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(_exercise())


# ----------------------------------------------------------------------
# Mid-turn stitch acknowledgement (issue #199). When a follow-up arrives
# while Alice is mid-turn for that same Signal channel, the producer
# diverts it into the active turn's context inbox. The sender otherwise
# has no signal back until Alice's reply lands (which can be a minute
# or more away), so the transport fires a reaction emoji on the inbound
# as a fire-and-forget cue.


class _RecordingRPC(_FakeRPC):
    """_FakeRPC + records every ``send_reaction`` call."""

    def __init__(self, envelopes: list[SignalEnvelope]) -> None:
        super().__init__(envelopes)
        self.reactions: list[dict] = []
        self.fail_next: bool = False

    async def send_reaction(
        self,
        *,
        recipient: str,
        target_author: str,
        target_timestamp: int,
        emoji: str,
    ) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("signal-cli blip")
        self.reactions.append(
            {
                "recipient": recipient,
                "target_author": target_author,
                "target_timestamp": target_timestamp,
                "emoji": emoji,
            }
        )


def test_react_calls_send_reaction_with_emoji():
    """react() forwards the emoji to the underlying signal-cli RPC."""

    async def _exercise() -> None:
        rpc = _RecordingRPC([])
        t = SignalTransport(signal_client=rpc)
        channel = ChannelRef(
            transport="signal", address="+15555550100", durable=True
        )
        await t.react(channel, target_timestamp=42, emoji="\U0001f441")
        assert rpc.reactions == [
            {
                "recipient": "+15555550100",
                "target_author": "+15555550100",
                "target_timestamp": 42,
                "emoji": "\U0001f441",
            }
        ]

    asyncio.run(_exercise())


def test_react_empty_emoji_is_noop():
    """Empty emoji = no RPC call. Lets callers pass config through
    without an extra branch when the operator disabled the ack."""

    async def _exercise() -> None:
        rpc = _RecordingRPC([])
        t = SignalTransport(signal_client=rpc)
        channel = ChannelRef(
            transport="signal", address="+15555550100", durable=True
        )
        await t.react(channel, target_timestamp=42, emoji="")
        assert rpc.reactions == []

    asyncio.run(_exercise())


def test_react_swallows_rpc_failure():
    """Reactions are cosmetic; an RPC blip must not propagate as an
    exception (or the stitch path would crash the daemon)."""

    async def _exercise() -> None:
        rpc = _RecordingRPC([])
        rpc.fail_next = True
        t = SignalTransport(signal_client=rpc)
        channel = ChannelRef(
            transport="signal", address="+15555550100", durable=True
        )
        # Must NOT raise.
        await t.react(channel, target_timestamp=42, emoji="\U0001f441")
        assert rpc.reactions == []

    asyncio.run(_exercise())


class _StitchCtx(_StubCtx):
    """_StubCtx + a divert_to_mid_turn hook and a cfg.speaking dict.

    Lets us exercise SignalTransport._produce's stitch path end-to-end
    without standing up a real SpeakingDaemon.
    """

    class _Cfg:
        def __init__(self, speaking: dict) -> None:
            self.speaking = speaking

    def __init__(self, ack_emoji: str = "\U0001f441") -> None:
        super().__init__()
        self.cfg = self._Cfg({"inbound_stitch_ack_emoji": ack_emoji})
        # Channel the divert hook treats as in-flight. Anything matching
        # it diverts; anything else falls through to the inbox.
        self.in_flight_channel = None
        self.diverted: list[tuple[ChannelRef, str]] = []

    def divert_to_mid_turn(
        self, channel: ChannelRef, text: str, _event
    ) -> bool:
        cur = self.in_flight_channel
        if cur is None:
            return False
        if (cur.transport, cur.address) != (channel.transport, channel.address):
            return False
        self.diverted.append((channel, text))
        return True


def test_produce_fires_ack_reaction_on_mid_turn_stitch():
    """When divert_to_mid_turn returns True, the producer must fire
    a reaction on the inbound's timestamp using the configured emoji.
    Issue #199 — visible 'seen' cue while the active turn is still
    composing the reply."""

    async def _exercise() -> None:
        envelopes = [
            SignalEnvelope(
                timestamp=777, source="+15555550100", body="follow-up"
            ),
        ]
        rpc = _RecordingRPC(envelopes)
        t = SignalTransport(signal_client=rpc)

        # Stub out _consume so the inbox doesn't try to run a real turn.
        async def _noop_consume(_ctx) -> None:
            while True:
                await asyncio.sleep(3600)

        t._consume = _noop_consume  # type: ignore[assignment]

        ctx = _StitchCtx(ack_emoji="\U0001f441")
        ctx.in_flight_channel = ChannelRef(
            transport="signal", address="+15555550100", durable=True
        )

        run_task = t.producer(ctx)
        assert run_task is not None
        try:
            # Wait until the reaction landed (fire-and-forget via
            # asyncio.create_task, so spin briefly to settle the task).
            for _ in range(50):
                if rpc.reactions:
                    break
                await asyncio.sleep(0.01)

            assert rpc.reactions == [
                {
                    "recipient": "+15555550100",
                    "target_author": "+15555550100",
                    "target_timestamp": 777,
                    "emoji": "\U0001f441",
                }
            ]
            # Diverted into the active turn — never queued to the inbox.
            assert t._inbox.empty()
            assert [text for _, text in ctx.diverted] == ["follow-up"]
        finally:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(_exercise())


def test_produce_skips_ack_when_emoji_empty():
    """Operator can disable the ack by setting the config to ''."""

    async def _exercise() -> None:
        envelopes = [
            SignalEnvelope(
                timestamp=888, source="+15555550100", body="follow-up"
            ),
        ]
        rpc = _RecordingRPC(envelopes)
        t = SignalTransport(signal_client=rpc)

        async def _noop_consume(_ctx) -> None:
            while True:
                await asyncio.sleep(3600)

        t._consume = _noop_consume  # type: ignore[assignment]

        ctx = _StitchCtx(ack_emoji="")
        ctx.in_flight_channel = ChannelRef(
            transport="signal", address="+15555550100", durable=True
        )

        run_task = t.producer(ctx)
        assert run_task is not None
        try:
            # Give the producer time to process the envelope.
            for _ in range(30):
                if ctx.diverted:
                    break
                await asyncio.sleep(0.01)
            # Stitch happened, but no reaction fired.
            assert ctx.diverted, "envelope should have been diverted"
            # Settle any pending tasks; reaction list should stay empty.
            await asyncio.sleep(0.05)
            assert rpc.reactions == []
        finally:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(_exercise())


def test_produce_does_not_ack_when_no_stitch():
    """Turn-starting inbounds get a normal reply turn — they should
    NOT get the stitch-ack reaction. The 'received' state emoji handled
    via set_message_state still fires from the consumer side; that's a
    different code path."""

    async def _exercise() -> None:
        envelopes = [
            SignalEnvelope(
                timestamp=999, source="+15555550100", body="fresh msg"
            ),
        ]
        rpc = _RecordingRPC(envelopes)
        t = SignalTransport(signal_client=rpc)

        async def _noop_consume(_ctx) -> None:
            while True:
                await asyncio.sleep(3600)

        t._consume = _noop_consume  # type: ignore[assignment]

        ctx = _StitchCtx(ack_emoji="\U0001f441")
        # No in-flight channel → divert returns False → normal queue path.
        ctx.in_flight_channel = None

        run_task = t.producer(ctx)
        assert run_task is not None
        try:
            for _ in range(30):
                if t._inbox.qsize() > 0:
                    break
                await asyncio.sleep(0.01)
            assert t._inbox.qsize() == 1
            # Settle and confirm no reaction was fired by the producer.
            await asyncio.sleep(0.05)
            assert rpc.reactions == []
            assert ctx.diverted == []
        finally:
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, BaseException):
                pass

    asyncio.run(_exercise())
