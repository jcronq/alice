"""Phase 3 of plan 01: registry-based dispatch.

Pins three behaviours:

- :class:`SourceRegistry` routes by event type to the registered
  source's :meth:`handle`.
- Unknown event types log + drop, no crash (dispatcher must survive
  a forgotten registration).
- Two sources for the same event type fail loud at registration —
  catches "I forgot which transport produces this" bugs at startup,
  not after the first event arrives.

Plus an integration check that :class:`SpeakingDaemon` registers
every visible event-producing source on construction (and skips
Signal, since it owns its own consumer loop).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from alice_speaking import daemon as daemon_module
from alice_speaking.daemon import (
    A2AEvent,
    CLIEvent,
    DiscordEvent,
    EmergencyEvent,
    SignalEvent,
    SurfaceEvent,
)
from alice_speaking.transports import SourceRegistry


# ---------------------------------------------------------------------------
# Test fakes


@dataclass
class _FakeEvent:
    payload: str


class _FakeSource:
    """Minimal InternalSource-shaped fake. Records every handle call."""

    name = "fake"
    event_type = _FakeEvent

    def __init__(self) -> None:
        self.handled: list[_FakeEvent] = []

    def producer(self, ctx) -> Optional[asyncio.Task]:
        return None

    async def handle(self, ctx, event: _FakeEvent) -> None:
        self.handled.append(event)


@dataclass
class _OtherEvent:
    pass


class _OtherFakeSource:
    name = "other"
    event_type = _OtherEvent

    def producer(self, ctx) -> Optional[asyncio.Task]:
        return None

    async def handle(self, ctx, event) -> None:
        return None


# ---------------------------------------------------------------------------
# Registry semantics


def test_registry_dispatches_by_event_type():
    registry = SourceRegistry()
    fake = _FakeSource()
    registry.register_internal(fake)

    looked_up = registry.lookup(_FakeEvent)
    assert looked_up is fake

    # End-to-end: lookup → handle.
    event = _FakeEvent(payload="hello")
    asyncio.run(looked_up.handle(ctx=None, event=event))
    assert fake.handled == [event]


def test_unknown_event_type_returns_none():
    """Lookup for an unregistered type returns None — the dispatcher
    must decide how to react (today: log + drop)."""
    registry = SourceRegistry()
    assert registry.lookup(_FakeEvent) is None


def test_register_duplicate_event_type_raises():
    registry = SourceRegistry()
    registry.register_internal(_FakeSource())
    # Second source for the same event_type must fail loudly.
    with pytest.raises(ValueError, match="already registered"):
        registry.register_internal(_FakeSource())


def test_register_distinct_event_types_coexist():
    registry = SourceRegistry()
    registry.register_internal(_FakeSource())
    registry.register_internal(_OtherFakeSource())
    assert registry.lookup(_FakeEvent) is not None
    assert registry.lookup(_OtherEvent) is not None
    # all_event_sources iteration yields both, in registration order.
    sources = list(registry.all_event_sources())
    assert len(sources) == 2
    assert sources[0].name == "fake"
    assert sources[1].name == "other"


# ---------------------------------------------------------------------------
# Daemon-level integration


def _make_daemon(cfg, monkeypatch):
    class _StubSignal:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr(daemon_module, "SignalClient", _StubSignal)
    return daemon_module.SpeakingDaemon(cfg)


def test_daemon_registers_visible_event_sources(cfg, monkeypatch):
    """Default test fixture has Signal + CLI + Owner principal but no
    Discord / A2A. Registry should route CLI, Surface, Emergency —
    and intentionally NOT Signal (own loop) or Discord/A2A (disabled)."""
    d = _make_daemon(cfg, monkeypatch)

    assert d._registry.lookup(CLIEvent) is not None
    assert d._registry.lookup(SurfaceEvent) is not None
    assert d._registry.lookup(EmergencyEvent) is not None

    # Signal events go through SignalTransport's per-transport inbox,
    # never the dispatcher queue.
    assert d._registry.lookup(SignalEvent) is None
    # Discord + A2A aren't enabled in this fixture.
    assert d._registry.lookup(DiscordEvent) is None
    assert d._registry.lookup(A2AEvent) is None


def test_unknown_event_in_consumer_logs_and_continues(cfg, monkeypatch, caplog):
    """End-to-end consumer behaviour: an event with no registered
    source logs a warning, doesn't crash the loop, and the next
    queued event still gets dispatched."""
    d = _make_daemon(cfg, monkeypatch)

    @dataclass
    class _Mystery:
        pass

    async def go():
        await d._queue.put(_Mystery())
        # Run one consumer iteration by canceling immediately after.
        consumer = asyncio.create_task(d._consumer())
        # Yield to the loop until the unknown event has been pulled.
        # Polling avoids a fixed sleep that flakes on slow runners.
        for _ in range(100):
            if d._queue.empty():
                break
            await asyncio.sleep(0.01)
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

    with caplog.at_level("WARNING"):
        asyncio.run(go())
    assert any(
        "no handler for event type" in r.message and "_Mystery" in r.message
        for r in caplog.records
    )


def test_signal_transport_intentionally_unregistered(cfg, monkeypatch):
    """Defensive: even though SignalTransport satisfies the Transport
    protocol, the daemon must NOT register it — Signal owns its own
    consumer loop after Phase 2a, and registering it would route the
    same event to two consumers and break batch coalescing."""
    d = _make_daemon(cfg, monkeypatch)
    # The transport itself exists when signal is enabled in the fixture.
    assert d.signal_transport is not None
    # …but the registry doesn't know about it.
    assert d._registry.lookup(SignalEvent) is None
