"""Tests for the A2A transport scaffolding.

Covers:
- Capability + agent-card construction (name-and-shape sanity).
- Outbound :meth:`A2ATransport.send` correctly routes to the per-task
  outbox the executor opened, and renders chunks per the A2A caps.
- The ``signal_done`` / ``signal_error`` sentinels enqueue the right
  shapes onto the outbox so the executor can translate them to A2A
  events.
- A live end-to-end smoke (uvicorn binds, A2A SSE stream produces a
  Task → status updates → artifacts → COMPLETED on the wire). This
  one is gated behind ``pytest -m live`` because it actually opens a
  socket; the unit tests cover the bridge logic deterministically.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from alice_speaking.transports.a2a import A2ATransport
from alice_speaking.transports.base import (
    A2A_CAPS,
    ChannelRef,
    OutboundMessage,
)


def test_caps_and_name_match_A2A_CAPS():
    t = A2ATransport(port=0)
    assert t.name == "a2a"
    assert t.caps is A2A_CAPS


def test_agent_card_basic_shape():
    t = A2ATransport(
        port=7878,
        agent_name="Alice",
        agent_description="test",
    )
    card = t._build_agent_card()
    assert card.name == "Alice"
    assert card.description == "test"
    assert card.capabilities.streaming is True
    # default in/out modes
    assert "text/plain" in card.default_input_modes
    assert "text/plain" in card.default_output_modes
    assert any(s.id == "conversation" for s in card.skills)
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC"


def test_agent_card_advertises_external_url_when_set():
    t = A2ATransport(port=9000, external_url="https://alice.example.com/a2a")
    card = t._build_agent_card()
    assert card.supported_interfaces[0].url == "https://alice.example.com/a2a"


def test_send_to_unknown_task_logs_and_drops(caplog):
    """No outbox open for the address → log + return 0 chunks delivered."""
    t = A2ATransport(port=0)

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(transport="a2a", address="ghost-task", durable=False),
                text="hello",
            )
        )

    with caplog.at_level("WARNING"):
        delivered = asyncio.run(go())
    assert delivered == 0
    assert any("no live task" in r.message for r in caplog.records)


def test_send_routes_chunks_to_open_outbox():
    """When the executor has opened an outbox for a task, send() pushes
    rendered chunks onto it."""
    t = A2ATransport(port=0)
    outbox = t._open_task_outbox("task-1")

    async def go():
        delivered = await t.send(
            OutboundMessage(
                destination=ChannelRef(transport="a2a", address="task-1", durable=False),
                text="**hello** world",
            )
        )
        return delivered

    delivered = asyncio.run(go())
    # full-markdown caps + small message → one chunk, content preserved
    assert delivered == 1
    ev = outbox.get_nowait()
    assert ev["kind"] == "chunk"
    assert "**hello**" in ev["text"]
    assert "world" in ev["text"]


def test_signal_done_enqueues_done_sentinel():
    t = A2ATransport(port=0)
    outbox = t._open_task_outbox("t1")

    asyncio.run(t.signal_done(ChannelRef(transport="a2a", address="t1", durable=False)))
    assert outbox.get_nowait() == {"kind": "done"}


def test_signal_error_enqueues_error_with_message():
    t = A2ATransport(port=0)
    outbox = t._open_task_outbox("t1")

    asyncio.run(
        t.signal_error(
            ChannelRef(transport="a2a", address="t1", durable=False),
            "kernel exploded",
        )
    )
    ev = outbox.get_nowait()
    assert ev == {"kind": "error", "message": "kernel exploded"}


def test_signal_done_silent_when_outbox_missing():
    """Late sentinel after executor finished → silent no-op (avoid raising)."""
    t = A2ATransport(port=0)
    # No _open_task_outbox call.
    asyncio.run(
        t.signal_done(ChannelRef(transport="a2a", address="ghost", durable=False))
    )  # should not raise


def test_typing_is_noop():
    t = A2ATransport(port=0)
    asyncio.run(
        t.typing(ChannelRef(transport="a2a", address="anything", durable=False), True)
    )  # should not raise


# -- live smoke ---------------------------------------------------------------

# Binds a real socket. Skipped in default suite; opt in with `pytest -m live`.
@pytest.mark.live
def test_live_request_response_smoke():
    """Spin up the transport, post a streaming SendStreamingMessage,
    drive the daemon-side reply via send + signal_done, verify the SSE
    stream carries TASK_STATE_SUBMITTED → WORKING → artifact → COMPLETED.
    """
    import httpx

    async def main():
        t = A2ATransport(port=0, host="127.0.0.1")
        # Find a free port: we let uvicorn bind to 0 by leaving the
        # port arg as the requested 17000+ range; here we just pick one
        # that's almost-certainly free for CI.
        t._port = 17891
        await t.start()

        async def fake_daemon():
            async for msg in t.messages():
                await t.send(
                    OutboundMessage(destination=msg.origin, text="echo: " + msg.text)
                )
                await t.signal_done(msg.origin)
                return

        daemon_task = asyncio.create_task(fake_daemon())
        events: list[dict] = []
        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers={"A2A-Version": "1.0"}
            ) as client:
                payload = {
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "SendStreamingMessage",
                    "params": {
                        "message": {
                            "messageId": "m1",
                            "role": "ROLE_USER",
                            "parts": [{"text": "hello alice"}],
                        }
                    },
                }
                async with client.stream(
                    "POST", f"http://127.0.0.1:{t._port}/", json=payload
                ) as r:
                    assert r.status_code == 200
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if line.startswith("data: "):
                            events.append(json.loads(line[6:]))
        finally:
            await daemon_task
            await t.stop()

        states = [
            ev["result"].get("statusUpdate", {}).get("status", {}).get("state")
            for ev in events
            if "statusUpdate" in ev.get("result", {})
        ]
        assert "TASK_STATE_WORKING" in states
        assert "TASK_STATE_COMPLETED" in states
        # At least one artifact frame containing our echo.
        assert any(
            "echo: hello alice"
            in (
                ev["result"].get("artifactUpdate", {})
                .get("artifact", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            for ev in events
            if "artifactUpdate" in ev.get("result", {})
        )

    asyncio.run(main())
