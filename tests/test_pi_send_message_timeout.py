"""PiSendMessageHandler: hung or raising `send_message` must not hang the handler.

Regression: a bare `await self._send_message(input)` with no timeout
was blocking the SDK tool-result dispatch whenever signal-cli stalled.
The tool call never resolved from the agent's view ("interrupted
before result received"), observed 5+ times in a single day.

Verified behaviour:
  1. Normal send: result propagates, event fires, is_error reflects
     the inner isError flag.
  2. send_message hangs forever: handler bounds the wait on
     _SEND_TIMEOUT_S, emits is_error=True with a timeout-shaped
     diagnostic. Does not block the enclosing task.
  3. send_message raises: handler captures the exception, emits
     is_error=True with the exception class + message.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from alice_speaking.turn_runner import PiSendMessageHandler


class _CapturingEvents:
    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    def emit(self, name: str, **fields: Any) -> None:
        self.emitted.append({"name": name, **fields})


@pytest.mark.asyncio
async def test_normal_send_returns_result_and_emits_event() -> None:
    async def sender(args: dict) -> dict:
        return {
            "content": [{"type": "text", "text": "sent via current channel (5 chars)"}]
        }

    events = _CapturingEvents()
    handler = PiSendMessageHandler(sender, events)
    await handler.on_tool_use(
        "mcp__alice__send_message", {"recipient": "self", "message": "hi"}, "call-1"
    )
    assert len(events.emitted) == 1
    ev = events.emitted[0]
    assert ev["name"] == "pi_send_message_result"
    assert ev["tool_call_id"] == "call-1"
    assert ev["is_error"] is False


@pytest.mark.asyncio
async def test_send_message_hang_times_out_without_blocking() -> None:
    """Hang-shaped sender: the handler must cap the wait and emit an
    error result, not stall forever."""

    async def hang(args: dict) -> dict:
        await asyncio.sleep(3600)  # pretend signal-cli is wedged
        return {}

    events = _CapturingEvents()
    handler = PiSendMessageHandler(hang, events)
    # Stub the class-level timeout short so the test finishes quickly.
    handler._SEND_TIMEOUT_S = 0.05

    await asyncio.wait_for(
        handler.on_tool_use(
            "send_message", {"recipient": "self", "message": "x"}, "call-2"
        ),
        timeout=5.0,
    )
    assert len(events.emitted) == 1
    ev = events.emitted[0]
    assert ev["is_error"] is True
    text = ev["result"]["content"][0]["text"]
    assert "timed out" in text


@pytest.mark.asyncio
async def test_send_message_exception_captured_as_error_result() -> None:
    class Boom(RuntimeError):
        pass

    async def boom(args: dict) -> dict:
        raise Boom("signal-cli refused")

    events = _CapturingEvents()
    handler = PiSendMessageHandler(boom, events)
    await handler.on_tool_use(
        "mcp__alice__send_message", {"recipient": "self", "message": "x"}, "call-3"
    )
    assert len(events.emitted) == 1
    ev = events.emitted[0]
    assert ev["is_error"] is True
    text = ev["result"]["content"][0]["text"]
    assert "Boom" in text
    assert "signal-cli refused" in text


@pytest.mark.asyncio
async def test_handler_ignores_non_send_message_tools() -> None:
    sent: list[dict] = []

    async def sender(args: dict) -> dict:
        sent.append(args)
        return {"content": [{"type": "text", "text": "ok"}]}

    events = _CapturingEvents()
    handler = PiSendMessageHandler(sender, events)
    await handler.on_tool_use("unrelated_tool", {}, "call-4")
    assert sent == []
    assert events.emitted == []
