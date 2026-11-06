"""Escape convention: ``//foo`` → literal ``/foo`` via rewritten_text.

The dispatcher returns a Response with ``rewritten_text`` set; the
caller mutates the head envelope's body and runs the normal turn
pipeline. The dispatch hook test confirms the body mutation happens.
"""

from __future__ import annotations

import asyncio
import types

from alice_speaking import slash_commands
from alice_speaking._dispatch import _maybe_dispatch_slash
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)
from alice_speaking.infra.signal_rpc import SignalEnvelope
from alice_speaking.transports.signal import SignalEvent


def _ctx(cfg):
    ns = types.SimpleNamespace()
    ns.cfg = cfg
    ns.signal_transport = None
    ns._queue = None
    ns._current_turn_kind = None
    ns._surface_watcher = None
    return ns


def test_double_slash_rewrites_to_single(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("//recall is literal", "jason", ctx))
    assert response is not None
    assert response.rewritten_text == "/recall is literal"
    assert response.reply_text is None


def test_double_slash_without_verb(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("//foo", "jason", ctx))
    assert response is not None
    assert response.rewritten_text == "/foo"


def test_dispatch_hook_mutates_envelope_on_escape(cfg):
    """When the dispatcher returns a rewritten_text, the hook mutates
    head.envelope.body in place and reports fall-through."""

    jason_book = AddressBook([
        PrincipalRecord(
            id="jason",
            display_name="Jason",
            channels=[
                PrincipalChannel(
                    transport="signal",
                    address="+15555550100",
                    durable=True,
                    preferred=True,
                ),
            ],
        ),
    ])
    ctx = _ctx(cfg)
    ctx.address_book = jason_book

    class _FakeTransport:
        def __init__(self):
            self.sent = []

        async def send(self, out):
            self.sent.append(out)
            return 1

        async def set_message_state(self, *args, **kwargs):
            return None

    ctx.signal_transport = _FakeTransport()

    class _Events:
        def emit(self, *args, **kwargs):
            pass

    ctx.events = _Events()

    envelope = SignalEnvelope(
        timestamp=1, source="+15555550100", body="//literal slash"
    )
    event = SignalEvent(envelope=envelope, sender_name="Jason")

    handled = asyncio.run(_maybe_dispatch_slash(ctx, event))
    assert handled is False  # caller must still run the normal pipeline
    assert envelope.body == "/literal slash"
    assert ctx.signal_transport.sent == []  # no direct reply sent
