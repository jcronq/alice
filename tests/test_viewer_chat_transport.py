"""Tests for the ViewerChatTransport.

Covers:
- Identity + capability sanity (mirrors test_a2a_transport.py).
- Inbound parsing — POST shape, default channel, validation.
- Outbound routing — send() pushes chunks to live subscribers,
  buffers when none, and updates history.
- Sentinels — signal_done / signal_error land on the SSE stream.
- Dispatcher integration — _produce() emits ViewerChatEvent objects
  carrying the right principal / channel.

The HTTP surface is exercised against the in-process Starlette app
via httpx ASGITransport so tests don't bind a real port.
"""

from __future__ import annotations

import asyncio

import httpx

from alice_speaking.transports.base import (
    ChannelRef,
    OutboundMessage,
)
from alice_speaking.transports.viewer_chat import (
    DEFAULT_CHANNEL_ID,
    VIEWER_CHAT_CAPS,
    ViewerChatEvent,
    ViewerChatTransport,
)


def _make_transport(**kwargs) -> ViewerChatTransport:
    """Construct a transport without binding a port. ``start()`` is
    intentionally NOT called — tests reach the Starlette app directly."""
    return ViewerChatTransport(port=0, **kwargs)


# ---------------------------------------------------------------------------
# Static surface


def test_name_caps_event_type():
    t = _make_transport()
    assert t.name == "viewer-chat"
    assert t.caps is VIEWER_CHAT_CAPS
    assert t.event_type is ViewerChatEvent
    # Caps shape: full markdown for the web client (renders via marked).
    assert VIEWER_CHAT_CAPS.markdown == "full"
    assert VIEWER_CHAT_CAPS.code_blocks is True


def test_default_channel_id_is_stable():
    """The default channel id is the v1 single-conversation anchor.
    Other code paths may key off this string, so it deserves a regression
    guard."""
    assert DEFAULT_CHANNEL_ID == "viewer-chat-main"


# ---------------------------------------------------------------------------
# Outbound — send()


def test_send_to_unknown_channel_buffers_and_does_not_drop():
    """No subscriber → broadcast lands in the per-channel buffer for
    replay on the next SSE connect."""
    t = _make_transport()

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="viewer-chat", address="viewer-chat-main", durable=True
                ),
                text="hello",
            )
        )

    delivered = asyncio.run(go())
    assert delivered == 1
    # Buffer holds one chunk for the missing subscriber.
    buf = t._buffer.get("viewer-chat-main")
    assert buf is not None and len(buf) == 1
    assert buf[0]["type"] == "chunk"
    assert "hello" in buf[0]["text"]


def test_send_routes_chunks_to_live_subscriber():
    """When a SSE subscriber is attached, send() fans the chunks out."""
    t = _make_transport()
    q = t._subscribe("viewer-chat-main")

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="viewer-chat", address="viewer-chat-main", durable=True
                ),
                text="**hi** there",
            )
        )

    delivered = asyncio.run(go())
    assert delivered == 1
    ev = q.get_nowait()
    assert ev["type"] == "chunk"
    assert "**hi**" in ev["text"]  # full markdown preserved


def test_send_updates_history():
    t = _make_transport()

    async def go():
        await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="viewer-chat", address="viewer-chat-main", durable=True
                ),
                text="reply payload",
            )
        )

    asyncio.run(go())
    hist = t.history_for("viewer-chat-main")
    assert len(hist) == 1
    assert hist[0]["role"] == "alice"
    assert hist[0]["text"] == "reply payload"


def test_send_unknown_channel_isolated_from_default():
    """A custom channel id keeps its history + buffer separate from
    the default. Multi-channel hook stays clean."""
    t = _make_transport()
    asyncio.run(
        t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="viewer-chat", address="alt-conv", durable=True
                ),
                text="other channel",
            )
        )
    )
    assert "alt-conv" in t._buffer
    assert "viewer-chat-main" not in t._buffer


# ---------------------------------------------------------------------------
# Sentinels


def test_signal_done_broadcasts_done():
    t = _make_transport()
    q = t._subscribe("viewer-chat-main")
    asyncio.run(
        t.signal_done(
            ChannelRef(transport="viewer-chat", address="viewer-chat-main", durable=True)
        )
    )
    assert q.get_nowait() == {"type": "done"}


def test_signal_error_broadcasts_error_with_message():
    t = _make_transport()
    q = t._subscribe("viewer-chat-main")
    asyncio.run(
        t.signal_error(
            ChannelRef(
                transport="viewer-chat", address="viewer-chat-main", durable=True
            ),
            "kernel exploded",
        )
    )
    ev = q.get_nowait()
    assert ev == {"type": "error", "message": "kernel exploded"}


def test_signal_done_silent_when_no_subscriber():
    """Late sentinel after the client closed the stream → silently buffered."""
    t = _make_transport()
    asyncio.run(
        t.signal_done(
            ChannelRef(transport="viewer-chat", address="ghost", durable=True)
        )
    )  # should not raise
    # Buffered for the (eventual) next subscriber.
    assert t._buffer.get("ghost") == [{"type": "done"}]


def test_typing_is_noop():
    t = _make_transport()
    asyncio.run(
        t.typing(
            ChannelRef(transport="viewer-chat", address="anything", durable=True), True
        )
    )  # should not raise


# ---------------------------------------------------------------------------
# Identity — principal / channel shape on inbound


def test_inbound_carries_configured_principal():
    t = _make_transport(
        principal_name="alice-jr",
        principal_display_name="Alice Jr",
    )
    msg = t._make_inbound(text="hello", channel_id="viewer-chat-main")
    assert msg.principal.transport == "viewer-chat"
    assert msg.principal.native_id == "alice-jr"
    assert msg.principal.display_name == "Alice Jr"
    assert msg.origin.transport == "viewer-chat"
    assert msg.origin.address == "viewer-chat-main"
    assert msg.origin.durable is True
    assert msg.text == "hello"


# ---------------------------------------------------------------------------
# HTTP surface — exercised via ASGITransport, no real socket


def _client(t: ViewerChatTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=t._build_app()),
        base_url="http://test",
    )


def test_http_send_rejects_missing_text():
    t = _make_transport()

    async def go():
        async with _client(t) as client:
            r = await client.post("/api/viewer-chat/send", json={})
            assert r.status_code == 400
            body = r.json()
            assert "text" in body["error"].lower()

    asyncio.run(go())


def test_http_send_rejects_non_object_body():
    t = _make_transport()

    async def go():
        async with _client(t) as client:
            r = await client.post("/api/viewer-chat/send", json=["array"])
            assert r.status_code == 400

    asyncio.run(go())


def test_http_send_rejects_empty_text():
    t = _make_transport()

    async def go():
        async with _client(t) as client:
            r = await client.post(
                "/api/viewer-chat/send", json={"text": "   "}
            )
            assert r.status_code == 400

    asyncio.run(go())


def test_http_send_pushes_to_inbox_and_acks():
    t = _make_transport()

    async def go():
        async with _client(t) as client:
            r = await client.post(
                "/api/viewer-chat/send",
                json={"text": "what's on today?"},
            )
            assert r.status_code == 202
            body = r.json()
            assert body["ok"] is True
            assert body["channel"] == "viewer-chat-main"
        # The inbound landed on the transport's inbox.
        msg = await asyncio.wait_for(t._inbox.get(), timeout=0.5)
        assert msg.text == "what's on today?"
        assert msg.origin.address == "viewer-chat-main"

    asyncio.run(go())


def test_http_send_honors_explicit_channel():
    t = _make_transport()

    async def go():
        async with _client(t) as client:
            r = await client.post(
                "/api/viewer-chat/send",
                json={"text": "hi alt", "channel": "alt-conv"},
            )
            assert r.status_code == 202
            assert r.json()["channel"] == "alt-conv"
        msg = await asyncio.wait_for(t._inbox.get(), timeout=0.5)
        assert msg.origin.address == "alt-conv"

    asyncio.run(go())


def test_http_history_returns_history_for_channel():
    t = _make_transport()

    async def go():
        # Pre-seed via send() so the history has known entries.
        await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="viewer-chat",
                    address="viewer-chat-main",
                    durable=True,
                ),
                text="seeded",
            )
        )
        async with _client(t) as client:
            r = await client.get("/api/viewer-chat/history")
            assert r.status_code == 200
            body = r.json()
            assert body["channel"] == "viewer-chat-main"
            assert len(body["messages"]) == 1
            assert body["messages"][0]["role"] == "alice"
            assert body["messages"][0]["text"] == "seeded"

    asyncio.run(go())


def test_http_health_returns_ok():
    t = _make_transport()

    async def go():
        async with _client(t) as client:
            r = await client.get("/api/viewer-chat/health")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["transport"] == "viewer-chat"
            assert body["default_channel"] == "viewer-chat-main"

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Dispatcher integration


def test_produce_pushes_viewer_chat_events_with_inbound():
    """``_produce(ctx)`` should pop one InboundMessage off ``messages()``
    per turn and put a :class:`ViewerChatEvent` on ``ctx._queue``."""
    t = _make_transport()

    class _Ctx:
        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

    ctx = _Ctx()

    async def go():
        # Seed the inbox the same way the HTTP handler would.
        inbound = t._make_inbound(text="from test", channel_id="viewer-chat-main")
        await t._inbox.put(inbound)
        task = asyncio.create_task(t._produce(ctx))
        try:
            event = await asyncio.wait_for(ctx._queue.get(), timeout=0.5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return event

    event = asyncio.run(go())
    assert isinstance(event, ViewerChatEvent)
    assert event.message.text == "from test"
    assert event.message.principal.transport == "viewer-chat"


# ---------------------------------------------------------------------------
# History cap


def test_history_cap_drops_oldest():
    t = _make_transport()
    t._history_max = 5

    async def go():
        for i in range(8):
            await t.send(
                OutboundMessage(
                    destination=ChannelRef(
                        transport="viewer-chat",
                        address="viewer-chat-main",
                        durable=True,
                    ),
                    text=f"msg {i}",
                )
            )

    asyncio.run(go())
    hist = t.history_for("viewer-chat-main")
    assert len(hist) == 5
    # The 8th message survived; the 0th is gone.
    assert hist[-1]["text"] == "msg 7"
    assert hist[0]["text"] == "msg 3"


def test_buffer_cap_drops_oldest():
    t = _make_transport(outbox_buffer_max=3)

    async def go():
        for i in range(7):
            await t.send(
                OutboundMessage(
                    destination=ChannelRef(
                        transport="viewer-chat",
                        address="viewer-chat-main",
                        durable=True,
                    ),
                    text=f"chunk {i}",
                )
            )

    asyncio.run(go())
    buf = t._buffer["viewer-chat-main"]
    assert len(buf) == 3
    assert buf[-1]["text"].endswith("chunk 6")
