"""Low-level sentinel-file helpers + pause-gate integration (issue #624).

The slice-2 state helpers in :mod:`alice_speaking.slash_state` write
sentinels to ``state_dir`` that the daemon's ingest path reads before
dispatching a non-slash turn. These tests isolate the file layer and
the ``handle_signal`` pause gate end-to-end.
"""

from __future__ import annotations

import asyncio
import datetime
import types

from alice_speaking import slash_state
from alice_speaking._dispatch import handle_signal
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)
from alice_speaking.infra.signal_rpc import SignalEnvelope
from alice_speaking.transports.signal import SignalEvent


def test_set_and_clear_paused_roundtrips(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    assert not slash_state.is_paused(state_dir=state_dir)

    path = slash_state.set_paused(
        principal_id="jason", reason="test", state_dir=state_dir
    )
    assert path.is_file()
    assert slash_state.is_paused(state_dir=state_dir)
    record = slash_state.read_pause_record(state_dir=state_dir)
    assert record and record["principal_id"] == "jason"

    cleared = slash_state.clear_paused(state_dir=state_dir)
    assert cleared is True
    assert not slash_state.is_paused(state_dir=state_dir)
    assert slash_state.clear_paused(state_dir=state_dir) is False


def test_quiet_until_expires(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    past = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=10)
    slash_state.set_quiet_until(
        until=past, principal_id="jason", state_dir=state_dir
    )
    # Expired deadline is reaped on read so the second call sees nothing.
    assert slash_state.quiet_until(state_dir=state_dir) is None
    assert not (state_dir / slash_state.QUIET_SENTINEL).is_file()


def test_quiet_until_is_returned_when_in_future(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    future = datetime.datetime.now().astimezone() + datetime.timedelta(minutes=30)
    slash_state.set_quiet_until(
        until=future, principal_id="jason", state_dir=state_dir
    )
    got = slash_state.quiet_until(state_dir=state_dir)
    assert got is not None
    assert (got - future).total_seconds() < 2  # round-trip is lossy by seconds


def test_record_interrupt_appends_jsonl(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    slash_state.record_interrupt(
        principal_id="jason", message="first", state_dir=state_dir
    )
    slash_state.record_interrupt(
        principal_id="jason", message="second", state_dir=state_dir
    )
    body = (state_dir / slash_state.INTERRUPT_SENTINEL).read_text()
    assert body.count("\n") == 2
    assert "first" in body
    assert "second" in body


# ---------------------------------------------------------------------------
# Pause gate — paused inbound (not a slash command) is dropped


class _FakeTransport:
    def __init__(self):
        self.sent = []
        self.typing_calls = []

    async def send(self, out):
        self.sent.append(out)
        return 1

    async def set_message_state(self, *args, **kwargs):
        return None

    async def typing(self, channel, on):
        self.typing_calls.append((channel, on))

    def build_prompt(self, *, sender_name, stamp, batch):
        return "prompt"


def _build_ctx(cfg, address_book):
    ns = types.SimpleNamespace()
    ns.cfg = cfg
    ns.address_book = address_book
    ns.signal_transport = _FakeTransport()
    ns._queue = None
    ns._current_turn_kind = None
    ns._current_reply_channel = None
    ns._current_principal_display_name = None
    ns._current_turn_replied = False
    ns._turn_did_send = False
    ns._turn_last_outbound = None
    ns._last_inbound = {}
    ns._idle_flushed = set()
    ns.turns = []
    ns.quiet_queue = types.SimpleNamespace(enqueue=lambda *a, **k: None)
    class _Events:
        def __init__(self):
            self.emitted = []

        def emit(self, *args, **kwargs):
            self.emitted.append((args, kwargs))

    ns.events = _Events()
    ns.turn_runner = None

    async def _flush(_channel):
        return None

    ns._flush_mid_turn_inbox = _flush

    async def _run_turn(*args, **kwargs):  # pragma: no cover — paused path skips this
        ns._run_turn_called = True

    ns._run_turn_called = False
    ns._run_turn = _run_turn
    return ns


def test_handle_signal_drops_inbound_when_paused(cfg):
    slash_state.set_paused(principal_id="jason", state_dir=cfg.state_dir)
    try:
        address_book = AddressBook(
            [
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
            ]
        )
        ctx = _build_ctx(cfg, address_book)
        envelope = SignalEnvelope(
            timestamp=1, source="+15555550100", body="hey what time is it"
        )
        event = SignalEvent(envelope=envelope, sender_name="Jason")
        asyncio.run(handle_signal(ctx, [event]))
        # Paused: no turn runs, no outbound is sent.
        assert ctx._run_turn_called is False
        assert ctx.signal_transport.sent == []
        # Idle tracker still refreshed so the session-close watcher re-arms.
        assert ("signal", "+15555550100") in ctx._last_inbound
        # Paused event was emitted.
        events = ctx.events.emitted
        assert any(a[0] == "signal_turn_paused" for a, _ in events)
    finally:
        slash_state.clear_paused(state_dir=cfg.state_dir)


def test_handle_signal_resume_passes_through_when_cleared(cfg):
    """Sanity: a /resume (slash intercept) STILL runs even when paused —
    tested via the slash-dispatch escape that pause doesn't block
    slash commands at all because the slash dispatcher runs FIRST."""
    slash_state.set_paused(principal_id="jason", state_dir=cfg.state_dir)
    try:
        address_book = AddressBook(
            [
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
            ]
        )
        ctx = _build_ctx(cfg, address_book)
        envelope = SignalEnvelope(
            timestamp=2, source="+15555550100", body="/resume"
        )
        event = SignalEvent(envelope=envelope, sender_name="Jason")
        asyncio.run(handle_signal(ctx, [event]))
        # /resume was dispatched, so a reply went out.
        assert len(ctx.signal_transport.sent) == 1
        assert "resumed" in ctx.signal_transport.sent[0].text.lower()
        # Pause sentinel cleared.
        assert not slash_state.is_paused(state_dir=cfg.state_dir)
    finally:
        slash_state.clear_paused(state_dir=cfg.state_dir)
