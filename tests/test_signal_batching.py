"""Tests for inbound message batching at the consumer.

When messages arrive while Alice is mid-turn, they queue up. On the next
consumer iteration she pulls all currently-queued messages from the same
sender and processes them as a single batched turn — same UX as Claude
Code's input queue.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

from alice_speaking import daemon as daemon_module
from alice_speaking.daemon import EmergencyEvent, SignalEvent, SurfaceEvent
from alice_speaking.signal_client import SignalEnvelope


def _make_daemon(cfg, monkeypatch):
    class _StubSignal:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr(daemon_module, "SignalClient", _StubSignal)
    return daemon_module.SpeakingDaemon(cfg)


def _sig(source: str, ts: int, body: str = "", name: str = "Owner") -> SignalEvent:
    return SignalEvent(
        envelope=SignalEnvelope(timestamp=ts, source=source, body=body),
        sender_name=name,
    )


def test_drain_signal_batch_returns_only_head_when_queue_empty(cfg, monkeypatch):
    d = _make_daemon(cfg, monkeypatch)
    head = _sig("+15555550100", 1, "first")
    batch = d._drain_signal_batch(head)
    assert len(batch) == 1
    assert batch[0] is head


def test_drain_signal_batch_collects_same_sender(cfg, monkeypatch):
    d = _make_daemon(cfg, monkeypatch)
    head = _sig("+15555550100", 1, "first")
    d._queue.put_nowait(_sig("+15555550100", 2, "second"))
    d._queue.put_nowait(_sig("+15555550100", 3, "third"))
    batch = d._drain_signal_batch(head)
    bodies = [ev.envelope.body for ev in batch]
    assert bodies == ["first", "second", "third"]
    assert d._queue.empty()


def test_drain_signal_batch_preserves_other_sender(cfg, monkeypatch):
    d = _make_daemon(cfg, monkeypatch)
    head = _sig("+15555550100", 1, "from owner")
    d._queue.put_nowait(_sig("+15555550100", 2, "also owner"))
    d._queue.put_nowait(_sig("+15555550101", 3, "from friend", name="Friend"))
    d._queue.put_nowait(_sig("+15555550100", 4, "more owner"))
    batch = d._drain_signal_batch(head)

    # Owner's three messages batch together; Friend's stays in the queue.
    assert [ev.envelope.body for ev in batch] == [
        "from owner",
        "also owner",
        "more owner",
    ]
    assert d._queue.qsize() == 1
    held = d._queue.get_nowait()
    assert held.envelope.body == "from friend"
    assert held.envelope.source == "+15555550101"


def test_drain_signal_batch_preserves_surface_and_emergency_events(cfg, monkeypatch, tmp_path):
    d = _make_daemon(cfg, monkeypatch)
    head = _sig("+15555550100", 1, "msg1")
    surface = SurfaceEvent(path=tmp_path / "s.md")
    emergency = EmergencyEvent(path=tmp_path / "e.md")
    d._queue.put_nowait(surface)
    d._queue.put_nowait(_sig("+15555550100", 2, "msg2"))
    d._queue.put_nowait(emergency)

    batch = d._drain_signal_batch(head)
    assert [ev.envelope.body for ev in batch] == ["msg1", "msg2"]
    # Held events restored in original order.
    assert d._queue.qsize() == 2
    first = d._queue.get_nowait()
    second = d._queue.get_nowait()
    assert first is surface
    assert second is emergency
