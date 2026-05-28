"""Integration tests for the WS gateway transport.

These tests bind a real listener on an ephemeral port so the full
HTTP-upgrade + auth gate path runs end-to-end (the bearer-rejection
path is meaningless if mocked out — half the point is "the upgrade
fails before any session is allocated").

Coverage:

- Static surface (name / caps / event_type) sanity.
- Direct happy-path: valid bearer → upgrade succeeds, ``message``
  inbound lands on the transport inbox.
- Auth gate: missing/bad bearer → handshake fails BEFORE any session
  is allocated (registry stays empty).
- Disconnect mid-turn: client drops while frames are queued → the
  per-connection slot in ``_connections`` is freed; no orphan.
- Round-trip: connect with the right token, send a message, drain
  acks + simulated chunks + done frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from alice_speaking.transports.base import ChannelRef, OutboundMessage
from alice_speaking.transports.ws import (
    DEFAULT_WS_PATH,
    WSEvent,
    WSTransport,
)


# ---------------------------------------------------------------------------
# Test rig
#
# Each test gets its own WSTransport bound on an ephemeral port. We
# pull the resolved port off ``transport.bound_port`` so tests never
# race on a hard-coded number.


async def _start_transport(token: str = "test-secret") -> WSTransport:
    t = WSTransport(host="127.0.0.1", port=0, token=token)
    await t.start()
    return t


async def _stop_transport(t: WSTransport) -> None:
    with contextlib.suppress(Exception):
        await t.stop()


def _ws_url(t: WSTransport) -> str:
    return f"ws://127.0.0.1:{t.bound_port}{DEFAULT_WS_PATH}"


# ---------------------------------------------------------------------------
# Static surface — sanity, no networking


def test_name_caps_event_type():
    """The transport advertises the expected dispatcher contract.

    Same shape as the other transport tests so a refactor that drops
    one of these obligations on the floor fails fast.
    """
    from alice_speaking.transports.base import CLI_CAPS

    t = WSTransport(host="127.0.0.1", port=0, token="dummy")
    assert t.name == "ws"
    assert t.caps is CLI_CAPS
    assert t.event_type is WSEvent


def test_construction_requires_token():
    """An empty token must be refused at construction time.

    The daemon's gate already checks this before constructing the
    transport, but the class itself must refuse to silently run
    without auth — a regression there would silently expose every
    inbound session.
    """
    with pytest.raises(ValueError):
        WSTransport(host="127.0.0.1", port=0, token="")


# ---------------------------------------------------------------------------
# Auth gate


def test_missing_bearer_rejects_upgrade_without_allocating_session():
    """A client that connects with NO Authorization header gets a 401
    response on the upgrade; nothing reaches ``_handle_connection``.

    This is the v1 contract from the issue: invalid bearer → close /
    refuse before completing the upgrade.
    """
    async def go():
        t = await _start_transport(token="right-secret")
        try:
            with pytest.raises(InvalidStatus) as exc_info:
                async with websockets.connect(_ws_url(t)):
                    pass
            # Exact code is 401 (chosen over 1008 to keep auth out of
            # the WS frame layer entirely).
            assert exc_info.value.response.status_code == 401
            # No session was allocated — the gate ran before
            # _handle_connection could run.
            assert t._connections == {}
        finally:
            await _stop_transport(t)

    asyncio.run(go())


def test_bad_bearer_rejects_upgrade():
    """A client that presents the wrong token is treated identically
    to one that presents none."""
    async def go():
        t = await _start_transport(token="right-secret")
        try:
            with pytest.raises(InvalidStatus) as exc_info:
                async with websockets.connect(
                    _ws_url(t),
                    additional_headers={"Authorization": "Bearer wrong-secret"},
                ):
                    pass
            assert exc_info.value.response.status_code == 401
            assert t._connections == {}
        finally:
            await _stop_transport(t)

    asyncio.run(go())


def test_wrong_path_rejects_upgrade():
    """Anything other than the configured path (``/cli``) gets 404."""
    async def go():
        t = await _start_transport(token="right-secret")
        try:
            url = f"ws://127.0.0.1:{t.bound_port}/somewhere-else"
            with pytest.raises(InvalidStatus) as exc_info:
                async with websockets.connect(
                    url,
                    additional_headers={"Authorization": "Bearer right-secret"},
                ):
                    pass
            assert exc_info.value.response.status_code == 404
        finally:
            await _stop_transport(t)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Happy path: handshake, inbound, ack, outbound


def test_round_trip_message_to_chunk_and_done():
    """Connect → send ``message`` → server acks → simulated kernel
    pushes a chunk via ``send()`` → daemon-side ``signal_done`` →
    client sees the full sequence.

    Stubs the kernel/turn — we're proving the wire works, not the
    Claude call. The session-allocation, ack, outbox send(), and
    signal_done paths are all exercised on the real transport.
    """
    async def go():
        t = await _start_transport(token="round-trip-secret")
        try:
            async with websockets.connect(
                _ws_url(t),
                additional_headers={"Authorization": "Bearer round-trip-secret"},
            ) as ws:
                await ws.send(json.dumps({"type": "message", "text": "ping"}))

                # First frame: ack.
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert first == {"type": "ack"}

                # The transport pushed an inbound onto its inbox. Pull
                # it out (the daemon would do this via the producer).
                inbound = await asyncio.wait_for(t._inbox.get(), timeout=2.0)
                assert inbound.text == "ping"
                assert inbound.principal.transport == "ws"
                channel = inbound.origin
                assert channel.transport == "ws"
                assert channel.address in t._connections

                # Simulate the kernel pushing a rendered reply chunk.
                delivered = await t.send(
                    OutboundMessage(
                        destination=channel,
                        text="pong",
                    )
                )
                assert delivered == 1

                # Client picks the chunk off the wire.
                chunk = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert chunk["type"] == "chunk"
                assert "pong" in chunk["text"]

                # End-of-turn sentinel.
                await t.signal_done(channel)
                done = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert done == {"type": "done"}
        finally:
            await _stop_transport(t)

    asyncio.run(go())


def test_bad_json_emits_error_without_closing():
    """Garbage on the wire → ``error`` frame, connection survives so
    the client can recover (mirrors CLI socket behavior)."""
    async def go():
        t = await _start_transport(token="recover-secret")
        try:
            async with websockets.connect(
                _ws_url(t),
                additional_headers={"Authorization": "Bearer recover-secret"},
            ) as ws:
                await ws.send("not json {{{")
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert ev["type"] == "error"
                assert "bad json" in ev["message"].lower()

                # Connection still alive: a follow-up message succeeds.
                await ws.send(json.dumps({"type": "message", "text": "hi"}))
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert ack == {"type": "ack"}
        finally:
            await _stop_transport(t)

    asyncio.run(go())


def test_empty_message_text_emits_error():
    """``message`` with empty/whitespace text → ``error`` frame, no
    inbound enqueued. Same shape as the CLI socket."""
    async def go():
        t = await _start_transport(token="empty-text-secret")
        try:
            async with websockets.connect(
                _ws_url(t),
                additional_headers={"Authorization": "Bearer empty-text-secret"},
            ) as ws:
                await ws.send(json.dumps({"type": "message", "text": "   "}))
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert ev["type"] == "error"
                assert t._inbox.empty()
        finally:
            await _stop_transport(t)

    asyncio.run(go())


def test_unknown_event_type_emits_error():
    """Unknown ``type`` → error frame, connection survives."""
    async def go():
        t = await _start_transport(token="unknown-event-secret")
        try:
            async with websockets.connect(
                _ws_url(t),
                additional_headers={"Authorization": "Bearer unknown-event-secret"},
            ) as ws:
                await ws.send(json.dumps({"type": "lol"}))
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert ev["type"] == "error"
                assert "unknown" in ev["message"].lower()
        finally:
            await _stop_transport(t)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Lifecycle: disconnect mid-turn must clean up registry


def test_disconnect_mid_turn_releases_connection_slot():
    """If a client connects, drives a turn, then drops, the per-
    connection slot in ``_connections`` is freed. No orphan sessions
    in the registry — the canonical regression Jason called out.
    """
    async def go():
        t = await _start_transport(token="cleanup-secret")
        try:
            async with websockets.connect(
                _ws_url(t),
                additional_headers={"Authorization": "Bearer cleanup-secret"},
            ) as ws:
                await ws.send(json.dumps({"type": "message", "text": "hi"}))
                # Drain ack so the server has fully accepted the
                # message; otherwise the close race can land before
                # the inbox put fires.
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert ack == {"type": "ack"}
                # One live connection right now.
                assert len(t._connections) == 1
            # WS client context exited → server-side connection loop
            # falls out of its async-iter and the finally block runs.
            # Give the event loop a turn to drain.
            for _ in range(20):
                if not t._connections:
                    break
                await asyncio.sleep(0.05)
            assert t._connections == {}
        finally:
            await _stop_transport(t)

    asyncio.run(go())


def test_send_to_dead_connection_is_noop():
    """A late send() to a conn_id that's already gone returns 0 and
    does not raise. Matches the CLI transport's "drop on missing
    writer" behavior."""
    async def go():
        t = await _start_transport(token="dead-secret")
        try:
            delivered = await t.send(
                OutboundMessage(
                    destination=ChannelRef(
                        transport="ws", address="never-existed", durable=False
                    ),
                    text="hello?",
                )
            )
            assert delivered == 0
        finally:
            await _stop_transport(t)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Dispatcher integration: _produce emits WSEvent objects


def test_produce_pushes_ws_events_with_inbound():
    """``_produce(ctx)`` should pop one InboundMessage off ``messages()``
    per turn and put a :class:`WSEvent` on ``ctx._queue``."""
    from alice_speaking.transports.base import Principal

    t = WSTransport(host="127.0.0.1", port=0, token="dispatcher-secret")

    class _Ctx:
        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

    ctx = _Ctx()

    async def go():
        from alice_speaking.transports.base import InboundMessage

        inbound = InboundMessage(
            principal=Principal(
                transport="ws", native_id="jason", display_name="Jason"
            ),
            origin=ChannelRef(transport="ws", address="abc123", durable=False),
            text="from test",
            timestamp=0.0,
        )
        await t._inbox.put(inbound)
        task = asyncio.create_task(t._produce(ctx))
        try:
            event = await asyncio.wait_for(ctx._queue.get(), timeout=0.5)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return event

    event = asyncio.run(go())
    assert isinstance(event, WSEvent)
    assert event.message.text == "from test"
    assert event.message.principal.transport == "ws"


def test_resolve_token_reads_env(monkeypatch):
    """``resolve_token`` returns whatever the configured env var
    contains (with whitespace trimmed). Daemon uses this at startup
    to decide whether to construct the transport at all."""
    from alice_speaking.transports.ws import resolve_token

    monkeypatch.setenv("ALICE_WS_GATEWAY_TOKEN", "  hello-token  ")
    assert resolve_token() == "hello-token"
    monkeypatch.delenv("ALICE_WS_GATEWAY_TOKEN")
    assert resolve_token() == ""

    # Custom env var name path — operators who don't like the default
    # variable name can point at their own.
    monkeypatch.setenv("MY_CUSTOM_TOKEN_VAR", "abc")
    assert resolve_token("MY_CUSTOM_TOKEN_VAR") == "abc"
