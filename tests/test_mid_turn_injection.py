"""Tests for mid-turn context injection.

Covers three layers, bound directly off the daemon class (no full
daemon boot):

1. ``SpeakingDaemon.divert_to_mid_turn`` — producer-side routing
   decision (same-channel in-flight → divert; else fall through).
2. ``SpeakingDaemon._posttooluse_hook`` — drain + format pending
   messages as ``additionalContext``.
3. ``SpeakingDaemon._flush_mid_turn_inbox`` — turn-end cleanup pushes
   leftover events back to the dispatcher queue.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from alice_speaking.daemon import SpeakingDaemon
from alice_speaking.transports.base import ChannelRef


# ---------------------------------------------------------------------------
# Stub scaffolding


class _StubEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


class _StubQueue:
    """Bare-bones asyncio.Queue.put_nowait surface."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def put_nowait(self, x: Any) -> None:
        self.items.append(x)


class _StubDaemon:
    """Minimal stand-in carrying the attributes the methods read.

    We attach the real daemon methods via ``__get__`` so the bound
    callables exercise the actual implementation under test.
    ``_channel_key`` is a staticmethod on the real daemon — copy the
    reference so ``self._channel_key(...)`` inside the methods works.
    """

    _channel_key = staticmethod(SpeakingDaemon._channel_key)

    def __init__(self) -> None:
        self.events = _StubEvents()
        self._queue = _StubQueue()
        self._mid_turn_inbox: dict[str, list[tuple[str, Any]]] = {}
        self._current_reply_channel: Optional[ChannelRef] = None
        self._current_principal_display_name: Optional[str] = None
        self._current_turn_replied: bool = False


def _bind(stub: _StubDaemon):
    """Bind the daemon's methods to a stub instance."""
    return {
        "divert_to_mid_turn": SpeakingDaemon.divert_to_mid_turn.__get__(
            stub, _StubDaemon
        ),
        "flush": SpeakingDaemon._flush_mid_turn_inbox.__get__(stub, _StubDaemon),
        "post_hook": SpeakingDaemon._posttooluse_hook.__get__(stub, _StubDaemon),
        "channel_key": SpeakingDaemon._channel_key,
    }


def _signal_channel(addr: str = "+15551234567") -> ChannelRef:
    return ChannelRef(transport="signal", address=addr, durable=True)


# ---------------------------------------------------------------------------
# divert_to_mid_turn


def test_divert_returns_false_when_no_in_flight_turn() -> None:
    stub = _StubDaemon()
    fns = _bind(stub)
    diverted = fns["divert_to_mid_turn"](_signal_channel(), "hi", object())
    assert diverted is False
    assert stub._mid_turn_inbox == {}


def test_divert_returns_true_for_same_channel_in_flight() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel("+15551234567")
    fns = _bind(stub)
    sentinel = object()
    diverted = fns["divert_to_mid_turn"](
        _signal_channel("+15551234567"), "follow-up", sentinel
    )
    assert diverted is True
    key = "signal:+15551234567"
    assert stub._mid_turn_inbox[key] == [("follow-up", sentinel)]


def test_divert_returns_false_for_different_channel() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel("+15551234567")
    fns = _bind(stub)
    diverted = fns["divert_to_mid_turn"](
        _signal_channel("+15559999999"), "from someone else", object()
    )
    assert diverted is False
    assert stub._mid_turn_inbox == {}


def test_divert_returns_false_for_different_transport() -> None:
    """Same numeric address but different transport = different channel."""
    stub = _StubDaemon()
    stub._current_reply_channel = ChannelRef(
        transport="signal", address="+15551234567", durable=True
    )
    fns = _bind(stub)
    diverted = fns["divert_to_mid_turn"](
        ChannelRef(transport="cli", address="+15551234567", durable=False),
        "from CLI",
        object(),
    )
    assert diverted is False


def test_divert_stops_after_send_message_flag_flips() -> None:
    """Once Alice has replied on the channel, conversation has rolled.
    Further inbound queues as next turn, doesn't inject."""
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    stub._current_turn_replied = True  # _send_message already fired
    fns = _bind(stub)
    diverted = fns["divert_to_mid_turn"](_signal_channel(), "late follow-up", object())
    assert diverted is False
    assert stub._mid_turn_inbox == {}


def test_divert_accumulates_multiple_messages() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    fns = _bind(stub)
    fns["divert_to_mid_turn"](_signal_channel(), "first", object())
    fns["divert_to_mid_turn"](_signal_channel(), "second", object())
    fns["divert_to_mid_turn"](_signal_channel(), "third", object())
    key = "signal:+15551234567"
    assert [t for t, _ in stub._mid_turn_inbox[key]] == ["first", "second", "third"]


def test_divert_emits_event_per_message() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    fns = _bind(stub)
    fns["divert_to_mid_turn"](_signal_channel(), "msg", object())
    kinds = [k for k, _ in stub.events.emitted]
    assert "mid_turn_inbound_diverted" in kinds


# ---------------------------------------------------------------------------
# _posttooluse_hook


def test_hook_returns_empty_when_no_pending() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    fns = _bind(stub)
    result = asyncio.run(fns["post_hook"]({"tool_name": "Bash"}, "toolu_1", None))
    assert result == {}


def test_hook_returns_empty_when_no_in_flight_channel() -> None:
    stub = _StubDaemon()
    fns = _bind(stub)
    result = asyncio.run(fns["post_hook"]({"tool_name": "Bash"}, "toolu_1", None))
    assert result == {}


def test_hook_drains_and_returns_additional_context() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    stub._current_principal_display_name = "Jason"
    stub._mid_turn_inbox["signal:+15551234567"] = [
        ("Oh and include Y", object()),
    ]
    fns = _bind(stub)
    result = asyncio.run(fns["post_hook"]({"tool_name": "Bash"}, "toolu_1", None))
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    ctx = out["additionalContext"]
    assert "Jason" in ctx
    assert "Oh and include Y" in ctx
    assert "follow-up message" in ctx
    # Inbox must be drained.
    assert stub._mid_turn_inbox.get("signal:+15551234567") is None


def test_hook_drains_multiple_messages_in_order() -> None:
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    stub._current_principal_display_name = "Jason"
    stub._mid_turn_inbox["signal:+15551234567"] = [
        ("first thought", object()),
        ("second thought", object()),
        ("third thought", object()),
    ]
    fns = _bind(stub)
    result = asyncio.run(fns["post_hook"]({"tool_name": "Bash"}, "toolu_1", None))
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "3 follow-up messages" in ctx
    idx_first = ctx.find("first thought")
    idx_second = ctx.find("second thought")
    idx_third = ctx.find("third thought")
    assert 0 < idx_first < idx_second < idx_third


def test_hook_skips_drain_after_send_message_replied() -> None:
    """Once Alice replied for this turn, the hook must NOT drain
    even if messages are sitting in the inbox (would be context the
    model can't act on)."""
    stub = _StubDaemon()
    stub._current_reply_channel = _signal_channel()
    stub._current_turn_replied = True
    stub._mid_turn_inbox["signal:+15551234567"] = [
        ("stranded message", object()),
    ]
    fns = _bind(stub)
    result = asyncio.run(fns["post_hook"]({"tool_name": "Bash"}, "toolu_1", None))
    assert result == {}
    # Messages stay in the inbox — turn-end flush will push them back to
    # the dispatcher queue as a fresh next-turn.
    assert len(stub._mid_turn_inbox["signal:+15551234567"]) == 1


# ---------------------------------------------------------------------------
# _flush_mid_turn_inbox


def test_flush_pushes_pending_events_to_dispatcher_queue() -> None:
    """Turn ends with messages still in mid_turn_inbox (e.g.,
    tool-less turn). They should go back onto the dispatcher queue
    so they become the next turn's prompt."""
    stub = _StubDaemon()
    sentinel_a = object()
    sentinel_b = object()
    stub._mid_turn_inbox["signal:+15551234567"] = [
        ("a", sentinel_a),
        ("b", sentinel_b),
    ]
    fns = _bind(stub)
    fns["flush"](_signal_channel())
    assert stub._queue.items == [sentinel_a, sentinel_b]
    assert stub._mid_turn_inbox == {}


def test_flush_with_empty_inbox_is_noop() -> None:
    stub = _StubDaemon()
    fns = _bind(stub)
    fns["flush"](_signal_channel())
    assert stub._queue.items == []


def test_flush_emits_event_with_count() -> None:
    stub = _StubDaemon()
    stub._mid_turn_inbox["signal:+15551234567"] = [
        ("a", object()),
        ("b", object()),
    ]
    fns = _bind(stub)
    fns["flush"](_signal_channel())
    flushed = [
        kw for k, kw in stub.events.emitted if k == "mid_turn_inbox_flushed"
    ]
    assert len(flushed) == 1
    assert flushed[0]["message_count"] == 2
