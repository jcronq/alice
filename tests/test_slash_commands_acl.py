"""ACL test: non-Jason principals can't trigger slash dispatch.

The dispatcher must log + decline even if the message starts with a
recognized verb. The direct-call test exercises the module-level
re-check; the integration test exercises the signal-ingest hook.
"""

from __future__ import annotations

import asyncio
import types

from alice_speaking import slash_commands
from alice_speaking._dispatch import _maybe_dispatch_slash
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


def test_non_jason_principal_rejected_at_module(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/help", "friend", ctx))
    assert response is None


def test_non_jason_principal_rejected_at_dispatch(cfg, address_book):
    """Integration: the signal-ingest hook refuses to dispatch for a
    non-Jason principal and reports fall-through to the caller."""

    # Build a bare-minimum ctx with an address_book that resolves
    # the sender to a non-Jason id.
    ctx = _ctx(cfg)
    ctx.address_book = address_book

    envelope = SignalEnvelope(
        timestamp=1,
        source="+15555550101",  # Friend, per the standard fixture
        body="/help",
    )
    event = SignalEvent(envelope=envelope, sender_name="Friend")

    handled = asyncio.run(_maybe_dispatch_slash(ctx, event))
    assert handled is False


def test_jason_principal_accepted_at_dispatch(cfg, address_book, monkeypatch):
    """Positive control: when the sender maps to jason the dispatcher
    returns True (the normal pipeline is skipped)."""

    # Patch the address book to map the fixture Owner number to jason.
    from alice_speaking.domain.principals import PrincipalRecord, PrincipalChannel, AddressBook

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

    # Fake transport that records what got sent.
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
        timestamp=1, source="+15555550100", body="/help"
    )
    event = SignalEvent(envelope=envelope, sender_name="Jason")

    handled = asyncio.run(_maybe_dispatch_slash(ctx, event))
    assert handled is True
    assert len(ctx.signal_transport.sent) == 1
    # Direct reply short-circuit: text comes from /help, so it must
    # mention the verb set.
    assert "/help" in ctx.signal_transport.sent[0].text
