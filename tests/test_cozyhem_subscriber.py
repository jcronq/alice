"""Tests for the CozyHemEventSubscriber InternalSource and its handler.

Covers:

- SSE frame parsing into :class:`CozyHemEvent` (kind / entity_id /
  payload extraction, malformed-data tolerance).
- Reconnect with exponential backoff: connection failure on the
  first attempt followed by a clean second attempt should sleep 1s,
  then 2s, then succeed; cap at 30s.
- :func:`handle_cozyhem_event` routes ``doorbell_pressed`` to a
  direct outbound dispatch via the address book's emergency
  recipient; unknown kinds are logged + dropped.

The httpx layer is replaced with a tiny in-process double so we
don't open real sockets.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

from alice_speaking._dispatch import handle_cozyhem_event
from alice_speaking.internal.cozyhem import (
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    CozyHemEvent,
    CozyHemEventSubscriber,
)
from alice_speaking.transports import ChannelRef


# ---------------------------------------------------------------------------
# Fake httpx layer
#
# Mirrors just enough of httpx.AsyncClient.stream() to drive the
# producer loop: an async context manager whose response.aiter_lines
# yields a scripted list of lines, then closes.


class _FakeResponse:
    def __init__(self, lines: list[str], *, raise_for_status_exc: Optional[Exception] = None):
        self._lines = lines
        self._raise_for_status_exc = raise_for_status_exc

    def raise_for_status(self) -> None:
        if self._raise_for_status_exc is not None:
            raise self._raise_for_status_exc

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeStreamCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, scripts: list):
        """scripts: list where each entry is either
        - a list[str] of SSE lines for one connection, OR
        - an Exception to raise from the ``stream()`` call.
        """
        self._scripts = scripts
        self._call = 0

    def stream(self, method, url, **kwargs):
        script = self._scripts[self._call]
        self._call += 1
        if isinstance(script, Exception):
            raise script
        return _FakeStreamCM(_FakeResponse(script))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _client_factory(scripts: list):
    """Return a factory callable that hands out a shared _FakeClient.

    Each call to the factory returns the SAME client so successive
    reconnects step through ``scripts`` in order.
    """
    client = _FakeClient(scripts)
    return lambda: client


# ---------------------------------------------------------------------------
# Stub ctx — only ``_queue`` and ``_stop`` are touched by the producer.


class _StubCtx:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()


# ---------------------------------------------------------------------------
# Producer / SSE parsing


def test_sse_frame_parsed_into_event() -> None:
    """One well-formed SSE frame becomes one CozyHemEvent on the queue."""
    payload = {"entity_id": "doorbell.front_door", "captured_at": 12345}
    lines = [
        "event: doorbell_pressed",
        f"data: {json.dumps(payload)}",
        "",  # blank line closes the event
    ]
    client_factory = _client_factory([lines])

    async def _run():
        ctx = _StubCtx()
        subscriber = CozyHemEventSubscriber(
            events_url="http://example/api/v1/events",
            http_client_factory=client_factory,
            sleep=lambda *_: asyncio.sleep(0),
        )
        task = asyncio.create_task(subscriber._run(ctx))
        try:
            event = await asyncio.wait_for(ctx._queue.get(), timeout=1.0)
        finally:
            ctx._stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return event

    event = asyncio.run(_run())
    assert isinstance(event, CozyHemEvent)
    assert event.kind == "doorbell_pressed"
    assert event.entity_id == "doorbell.front_door"
    assert event.payload == payload
    assert event.received_at > 0


def test_multiple_frames_yield_multiple_events() -> None:
    """Two SSE frames in one connection produce two queue entries."""
    p1 = {"entity_id": "doorbell.front_door"}
    p2 = {"entity_id": "motion.driveway"}
    lines = [
        "event: doorbell_pressed",
        f"data: {json.dumps(p1)}",
        "",
        "event: motion_detected",
        f"data: {json.dumps(p2)}",
        "",
    ]
    client_factory = _client_factory([lines])

    async def _run():
        ctx = _StubCtx()
        subscriber = CozyHemEventSubscriber(
            events_url="http://example/api/v1/events",
            http_client_factory=client_factory,
            sleep=lambda *_: asyncio.sleep(0),
        )
        task = asyncio.create_task(subscriber._run(ctx))
        out = []
        try:
            out.append(await asyncio.wait_for(ctx._queue.get(), timeout=1.0))
            out.append(await asyncio.wait_for(ctx._queue.get(), timeout=1.0))
        finally:
            ctx._stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return out

    events = asyncio.run(_run())
    assert [e.kind for e in events] == ["doorbell_pressed", "motion_detected"]
    assert events[0].entity_id == "doorbell.front_door"
    assert events[1].entity_id == "motion.driveway"


def test_sse_comment_lines_ignored() -> None:
    """SSE heartbeat comment lines (starting with ':') don't break parsing."""
    lines = [
        ": heartbeat",
        "event: doorbell_pressed",
        ': keepalive',
        'data: {"entity_id": "doorbell.front_door"}',
        "",
    ]
    client_factory = _client_factory([lines])

    async def _run():
        ctx = _StubCtx()
        subscriber = CozyHemEventSubscriber(
            events_url="http://example/api/v1/events",
            http_client_factory=client_factory,
            sleep=lambda *_: asyncio.sleep(0),
        )
        task = asyncio.create_task(subscriber._run(ctx))
        try:
            event = await asyncio.wait_for(ctx._queue.get(), timeout=1.0)
        finally:
            ctx._stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return event

    event = asyncio.run(_run())
    assert event.kind == "doorbell_pressed"
    assert event.entity_id == "doorbell.front_door"


def test_malformed_data_yields_empty_payload(caplog) -> None:
    """A non-JSON ``data:`` body shouldn't crash the producer — emit
    an event with an empty payload + warn."""
    lines = [
        "event: doorbell_pressed",
        "data: not-json-{",
        "",
    ]
    client_factory = _client_factory([lines])

    async def _run():
        ctx = _StubCtx()
        subscriber = CozyHemEventSubscriber(
            events_url="http://example/api/v1/events",
            http_client_factory=client_factory,
            sleep=lambda *_: asyncio.sleep(0),
        )
        task = asyncio.create_task(subscriber._run(ctx))
        try:
            event = await asyncio.wait_for(ctx._queue.get(), timeout=1.0)
        finally:
            ctx._stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return event

    with caplog.at_level("WARNING"):
        event = asyncio.run(_run())
    assert event.kind == "doorbell_pressed"
    assert event.entity_id == ""
    assert event.payload == {}


def test_reconnect_uses_exponential_backoff() -> None:
    """Failing twice then succeeding should sleep 1s, then 2s, then yield
    the event."""
    payload = {"entity_id": "doorbell.front_door"}
    success_lines = [
        "event: doorbell_pressed",
        f"data: {json.dumps(payload)}",
        "",
    ]
    # Script: two failures (RuntimeErrors), then a successful stream.
    scripts: list = [
        RuntimeError("connection refused"),
        RuntimeError("connection refused"),
        success_lines,
    ]
    client_factory = _client_factory(scripts)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Return immediately so the test doesn't burn wall clock.
        await asyncio.sleep(0)

    async def _run():
        ctx = _StubCtx()
        subscriber = CozyHemEventSubscriber(
            events_url="http://example/api/v1/events",
            http_client_factory=client_factory,
            sleep=fake_sleep,
        )
        task = asyncio.create_task(subscriber._run(ctx))
        try:
            event = await asyncio.wait_for(ctx._queue.get(), timeout=1.0)
        finally:
            ctx._stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return event

    event = asyncio.run(_run())
    assert event.kind == "doorbell_pressed"
    # Two failures → two backoff sleeps with 1s then 2s.
    assert sleep_calls[:2] == [BACKOFF_INITIAL_SECONDS, BACKOFF_INITIAL_SECONDS * 2.0]


def test_backoff_capped_at_max() -> None:
    """Many consecutive failures should cap the backoff at BACKOFF_MAX_SECONDS."""
    # Six failures should ramp 1, 2, 4, 8, 16, 30 (capped).
    fails = [RuntimeError("nope")] * 6
    success_lines = [
        "event: doorbell_pressed",
        'data: {"entity_id": "x"}',
        "",
    ]
    client_factory = _client_factory(fails + [success_lines])
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        await asyncio.sleep(0)

    async def _run():
        ctx = _StubCtx()
        subscriber = CozyHemEventSubscriber(
            events_url="http://example/api/v1/events",
            http_client_factory=client_factory,
            sleep=fake_sleep,
        )
        task = asyncio.create_task(subscriber._run(ctx))
        try:
            await asyncio.wait_for(ctx._queue.get(), timeout=1.0)
        finally:
            ctx._stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())
    # First six sleeps should ramp through the geometric series and
    # then clamp at the cap on the sixth (would have been 32 without
    # the cap). Anything observed beyond that is from the post-success
    # reconnect path and isn't what this test is asserting on.
    assert sleep_calls[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, BACKOFF_MAX_SECONDS]
    # Verify the cap actually clamped every observed sleep.
    assert all(s <= BACKOFF_MAX_SECONDS for s in sleep_calls)


# ---------------------------------------------------------------------------
# Handler stub — handle_cozyhem_event


@dataclass
class _StubEvents:
    emitted: list[tuple[str, dict]] = field(default_factory=list)

    def emit(self, event_name: str, **kwargs) -> None:
        self.emitted.append((event_name, kwargs))


class _StubAddressBook:
    def __init__(self, recipient: Optional[ChannelRef]) -> None:
        self._recipient = recipient

    def emergency_recipient(self) -> Optional[ChannelRef]:
        return self._recipient


class _HandlerCtx:
    def __init__(self, recipient: Optional[ChannelRef] = None) -> None:
        self.events = _StubEvents()
        self.address_book = _StubAddressBook(recipient)
        self.dispatched: list[dict] = []
        self.dispatch_should_raise: Optional[Exception] = None

    async def _dispatch_outbound(
        self,
        channel: ChannelRef,
        text: str,
        attachments,
        *,
        emergency: bool = False,
        bypass_quiet: bool = False,
        turn_id=None,
    ) -> None:
        self.dispatched.append(
            {
                "channel": channel,
                "text": text,
                "attachments": attachments,
                "emergency": emergency,
                "bypass_quiet": bypass_quiet,
            }
        )
        if self.dispatch_should_raise is not None:
            raise self.dispatch_should_raise


def test_handler_routes_doorbell_pressed_to_emergency_recipient() -> None:
    recipient = ChannelRef(
        transport="signal", address="+15551234567", durable=True
    )
    ctx = _HandlerCtx(recipient=recipient)
    event = CozyHemEvent(
        kind="doorbell_pressed",
        entity_id="doorbell.front_door",
        payload={"entity_id": "doorbell.front_door"},
        received_at=123.0,
    )

    asyncio.run(handle_cozyhem_event(ctx, event))

    assert len(ctx.dispatched) == 1
    sent = ctx.dispatched[0]
    assert sent["channel"] == recipient
    assert "Doorbell pressed" in sent["text"]
    assert "doorbell.front_door" in sent["text"]
    assert sent["bypass_quiet"] is True
    kinds = [k for k, _ in ctx.events.emitted]
    assert "cozyhem_event" in kinds
    assert "cozyhem_doorbell_voiced" in kinds


def test_handler_drops_unknown_kinds() -> None:
    recipient = ChannelRef(
        transport="signal", address="+15551234567", durable=True
    )
    ctx = _HandlerCtx(recipient=recipient)
    event = CozyHemEvent(
        kind="motion_detected",
        entity_id="motion.driveway",
        payload={},
        received_at=123.0,
    )

    asyncio.run(handle_cozyhem_event(ctx, event))

    # Unknown kind = log + drop, no outbound send.
    assert ctx.dispatched == []
    kinds = [k for k, _ in ctx.events.emitted]
    assert "cozyhem_event" in kinds
    assert "cozyhem_doorbell_voiced" not in kinds


def test_handler_skips_send_when_no_recipient(caplog) -> None:
    ctx = _HandlerCtx(recipient=None)
    event = CozyHemEvent(
        kind="doorbell_pressed",
        entity_id="doorbell.front_door",
        payload={},
        received_at=123.0,
    )

    with caplog.at_level("WARNING"):
        asyncio.run(handle_cozyhem_event(ctx, event))

    assert ctx.dispatched == []
    kinds = [k for k, _ in ctx.events.emitted]
    assert "cozyhem_doorbell_no_recipient" in kinds


def test_handler_emits_error_event_when_send_fails() -> None:
    recipient = ChannelRef(
        transport="signal", address="+15551234567", durable=True
    )
    ctx = _HandlerCtx(recipient=recipient)
    ctx.dispatch_should_raise = RuntimeError("signal-cli unreachable")
    event = CozyHemEvent(
        kind="doorbell_pressed",
        entity_id="doorbell.front_door",
        payload={},
        received_at=123.0,
    )

    asyncio.run(handle_cozyhem_event(ctx, event))

    # Dispatch was attempted but failed.
    assert len(ctx.dispatched) == 1
    kinds = [k for k, _ in ctx.events.emitted]
    assert "cozyhem_doorbell_send_error" in kinds
    assert "cozyhem_doorbell_voiced" not in kinds
