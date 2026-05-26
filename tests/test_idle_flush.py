"""Tests for the session-close idle-flush protocol (issue #373).

Three layers exercised:

1. The producer loop in :class:`IdleFlushSource` — feed it a stale
   ``_last_inbound`` timestamp and verify it emits one
   :class:`IdleEvent`, sets the flush-flag, and does NOT re-fire on the
   next tick.
2. The inbound touchpoint helper ``_touch_inbound`` — verify it
   refreshes ``_last_inbound`` and clears any prior flush-flag for the
   same key.
3. The handler :func:`handle_idle` — verify it runs ``_run_turn(...,
   silent=True)``, restores ``_current_turn_kind`` in the finally
   block, and emits the start/end events with the expected payload.

A stub ctx mocks just enough of :class:`DaemonContext` to drive the
producer + handler without booting a full daemon, matching the pattern
in :mod:`tests.test_background_task_completion`.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass, field
from typing import Optional

import pytest

from alice_speaking._dispatch import _touch_inbound, handle_idle
from alice_speaking.internal import IDLE_POLL_SECONDS, IdleEvent, IdleFlushSource


# ---------------------------------------------------------------------------
# Stubs


@dataclass
class _StubEvents:
    emitted: list[tuple[str, dict]] = field(default_factory=list)

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


class _StubAddressBook:
    """display_name_for: synthesize "<Name>" from native_id."""

    def __init__(self, mapping: Optional[dict[tuple[str, str], str]] = None) -> None:
        self._mapping = mapping or {}

    def display_name_for(self, transport: str, native_id: str) -> str:
        return self._mapping.get(
            (transport, native_id),
            f"<{transport}:{native_id}>",
        )


class _StubStop:
    """asyncio.Event-shaped flag the producer polls on each cycle."""

    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


class _StubCtx:
    """Minimal DaemonContext that exposes only what the producer and
    handler read."""

    def __init__(
        self,
        *,
        speaking_cfg: Optional[dict] = None,
        address_book: Optional[_StubAddressBook] = None,
    ) -> None:
        self.cfg = type("_Cfg", (), {})()
        self.cfg.speaking = dict(speaking_cfg or {})
        self.address_book = address_book or _StubAddressBook()
        self.events = _StubEvents()
        self._stop = _StubStop()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._last_inbound: dict[tuple[str, str], datetime.datetime] = {}
        self._idle_flushed: set[tuple[str, str]] = set()
        # Handler-only fields:
        self._current_turn_kind: Optional[str] = "previous"
        self.run_turn_calls: list[dict] = []
        self.run_turn_should_raise: Optional[Exception] = None
        self.kind_at_run_turn: Optional[str] = None

    async def _run_turn(
        self,
        prompt: str,
        *,
        turn_id: str,
        outbound_recipient,
        silent: bool = False,
    ) -> str:
        self.run_turn_calls.append(
            {
                "prompt": prompt,
                "turn_id": turn_id,
                "outbound_recipient": outbound_recipient,
                "silent": silent,
            }
        )
        self.kind_at_run_turn = self._current_turn_kind
        if self.run_turn_should_raise is not None:
            raise self.run_turn_should_raise
        return ""


# ---------------------------------------------------------------------------
# _touch_inbound — sanity around the inbound hook


def test_touch_inbound_stamps_now_and_clears_flush_flag() -> None:
    ctx = _StubCtx()
    key = ("signal", "+15551234567")
    # Pretend a prior flush already fired for this key — the next
    # inbound should clear it so the watcher re-arms.
    ctx._idle_flushed.add(key)
    ctx._last_inbound[key] = datetime.datetime.now().astimezone() - datetime.timedelta(
        days=1
    )

    _touch_inbound(ctx, "signal", "+15551234567")

    assert key not in ctx._idle_flushed
    # Newly stamped timestamp is within a second of now.
    delta = datetime.datetime.now().astimezone() - ctx._last_inbound[key]
    assert delta.total_seconds() < 1.0


# ---------------------------------------------------------------------------
# Producer loop


async def _step_producer_once(source: IdleFlushSource, ctx: _StubCtx) -> None:
    """Drive one iteration of the producer loop by patching the
    sleep — keeps the test deterministic and fast.

    Implementation note: the loop's first action is ``await
    asyncio.sleep(IDLE_POLL_SECONDS)``. Rather than wait 60 real
    seconds, we monkey-patch ``asyncio.sleep`` so the first call
    returns immediately; the second call (the *next* poll) blocks long
    enough that we can cancel the task. We use a single task and
    cancel it after the work is done so any unfired emit doesn't
    survive into the next test.
    """
    sleep_calls = {"n": 0}
    original_sleep = asyncio.sleep

    async def _fast_sleep(seconds: float, result=None) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            # First poll cycle — return immediately.
            return None
        # Subsequent cycles — block long enough for the test to
        # observe state and cancel us.
        return await original_sleep(10.0)

    # Patch on the module the source actually imports from.
    import alice_speaking.internal.idle as idle_module

    real_sleep = idle_module.asyncio.sleep
    idle_module.asyncio.sleep = _fast_sleep  # type: ignore[assignment]
    try:
        task = asyncio.create_task(source._run(ctx))
        # Yield enough times for the producer's body to run after the
        # zero-sleep returns. ``asyncio.sleep(0)`` gives the loop a
        # chance to schedule other tasks; a few yields cover the
        # async dict access + queue put.
        for _ in range(5):
            await original_sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        idle_module.asyncio.sleep = real_sleep  # type: ignore[assignment]


def test_producer_emits_idle_event_for_stale_inbound() -> None:
    """A channel whose last inbound is older than the configured
    timeout produces one :class:`IdleEvent` and sets the flush flag."""

    async def runner() -> None:
        ctx = _StubCtx(
            speaking_cfg={"session_close_timeout_minutes": 10},
            address_book=_StubAddressBook(
                mapping={("signal", "+15551234567"): "Jason"}
            ),
        )
        stale = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=15)
        ctx._last_inbound[("signal", "+15551234567")] = stale

        source = IdleFlushSource()
        await _step_producer_once(source, ctx)

        assert ctx._queue.qsize() == 1
        ev = ctx._queue.get_nowait()
        assert isinstance(ev, IdleEvent)
        assert ev.transport == "signal"
        assert ev.sender_name == "Jason"
        assert ev.idle_since == stale
        # Flush flag is set so the next poll cycle does NOT re-fire.
        assert ("signal", "+15551234567") in ctx._idle_flushed

    asyncio.run(runner())


def test_producer_does_not_refire_when_already_flushed() -> None:
    """A channel already in ``_idle_flushed`` is skipped even when its
    timestamp is stale — fires once per silence gap, not per poll."""

    async def runner() -> None:
        ctx = _StubCtx(speaking_cfg={"session_close_timeout_minutes": 10})
        key = ("signal", "+15551234567")
        ctx._last_inbound[key] = datetime.datetime.now().astimezone() - datetime.timedelta(
            minutes=30
        )
        ctx._idle_flushed.add(key)

        source = IdleFlushSource()
        await _step_producer_once(source, ctx)

        assert ctx._queue.empty()

    asyncio.run(runner())


def test_producer_skips_fresh_inbound() -> None:
    """A channel touched within the timeout window does NOT fire."""

    async def runner() -> None:
        ctx = _StubCtx(speaking_cfg={"session_close_timeout_minutes": 10})
        ctx._last_inbound[("signal", "+15551234567")] = (
            datetime.datetime.now().astimezone() - datetime.timedelta(minutes=5)
        )

        source = IdleFlushSource()
        await _step_producer_once(source, ctx)

        assert ctx._queue.empty()
        assert ("signal", "+15551234567") not in ctx._idle_flushed

    asyncio.run(runner())


def test_producer_uses_default_timeout_when_config_missing() -> None:
    """Missing ``session_close_timeout_minutes`` → default 10 minutes."""

    async def runner() -> None:
        ctx = _StubCtx(speaking_cfg={})
        stale = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=15)
        ctx._last_inbound[("signal", "+15551234567")] = stale

        source = IdleFlushSource()
        await _step_producer_once(source, ctx)

        assert ctx._queue.qsize() == 1

    asyncio.run(runner())


def test_producer_hot_reloads_timeout() -> None:
    """The timeout is read on each cycle so a hot-reload takes effect
    immediately. Set a huge timeout → no fire; shrink it → fire."""

    async def runner() -> None:
        ctx = _StubCtx(speaking_cfg={"session_close_timeout_minutes": 60})
        stale = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=15)
        ctx._last_inbound[("signal", "+15551234567")] = stale

        source = IdleFlushSource()
        await _step_producer_once(source, ctx)
        # 15 min < 60 min ⇒ no fire.
        assert ctx._queue.empty()

        # Hot-reload: shrink the timeout below the actual idle window.
        ctx.cfg.speaking["session_close_timeout_minutes"] = 10
        await _step_producer_once(source, ctx)
        assert ctx._queue.qsize() == 1

    asyncio.run(runner())


def test_producer_survives_bad_config_value() -> None:
    """A non-numeric timeout doesn't crash the producer; it falls
    back to the default."""

    async def runner() -> None:
        ctx = _StubCtx(speaking_cfg={"session_close_timeout_minutes": "bogus"})
        stale = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=15)
        ctx._last_inbound[("signal", "+15551234567")] = stale

        source = IdleFlushSource()
        await _step_producer_once(source, ctx)

        # Default 10 ⇒ 15 min idle should fire.
        assert ctx._queue.qsize() == 1

    asyncio.run(runner())


# ---------------------------------------------------------------------------
# IdleFlushSource shape


def test_source_registers_event_type_and_name() -> None:
    """Sanity: the source's class attributes match the registry's
    expectations (event_type used as dispatch key; name surfaced in
    logs)."""
    source = IdleFlushSource()
    assert source.event_type is IdleEvent
    assert source.name == "idle_flush"


def test_idle_poll_seconds_default() -> None:
    """Sanity guard against a typo'd cadence constant."""
    assert IDLE_POLL_SECONDS == 60.0


# ---------------------------------------------------------------------------
# handle_idle


def _make_event(
    sender_name: str = "Jason",
    transport: str = "signal",
    idle_minutes: int = 12,
) -> IdleEvent:
    idle_since = datetime.datetime.now().astimezone() - datetime.timedelta(
        minutes=idle_minutes
    )
    return IdleEvent(
        sender_name=sender_name,
        transport=transport,
        idle_since=idle_since,
    )


def test_handler_runs_silent_turn_with_no_outbound() -> None:
    """The flush turn is silent (no Signal sends) and has no outbound
    recipient — confirms the design's promise of "purely internal."""
    ctx = _StubCtx()
    event = _make_event()

    asyncio.run(handle_idle(ctx, event))

    assert len(ctx.run_turn_calls) == 1
    call = ctx.run_turn_calls[0]
    assert call["silent"] is True
    assert call["outbound_recipient"] is None
    assert call["turn_id"]


def test_handler_sets_idle_flush_turn_kind_while_running() -> None:
    """During the turn ``_current_turn_kind`` is ``idle_flush``; the
    finally block restores the prior value."""
    ctx = _StubCtx()
    event = _make_event()

    asyncio.run(handle_idle(ctx, event))

    assert ctx.kind_at_run_turn == "idle_flush"
    assert ctx._current_turn_kind == "previous"


def test_handler_emits_start_and_end_events() -> None:
    """Both bracket events fire with the expected payload shape."""
    ctx = _StubCtx()
    event = _make_event(sender_name="Jason", idle_minutes=12)

    asyncio.run(handle_idle(ctx, event))

    kinds = [name for name, _ in ctx.events.emitted]
    assert "session_close_flush_start" in kinds
    assert "session_close_flush_end" in kinds

    start_payload = dict(
        next(payload for name, payload in ctx.events.emitted if name == "session_close_flush_start")
    )
    assert start_payload["sender_name"] == "Jason"
    assert start_payload["idle_minutes"] == 12
    assert start_payload["turn_id"]

    end_payload = dict(
        next(payload for name, payload in ctx.events.emitted if name == "session_close_flush_end")
    )
    assert end_payload["sender_name"] == "Jason"
    assert end_payload["idle_minutes"] == 12
    assert end_payload["error"] is None
    assert end_payload["duration_ms"] >= 0
    # Same turn_id on both events so an analyst can pair them.
    assert end_payload["turn_id"] == start_payload["turn_id"]


def test_handler_records_error_on_turn_exception() -> None:
    """An exception inside ``_run_turn`` is captured in the end event
    rather than escaping; the consumer keeps draining."""
    ctx = _StubCtx()
    ctx.run_turn_should_raise = RuntimeError("kaboom")
    event = _make_event()

    asyncio.run(handle_idle(ctx, event))

    end_payload = dict(
        next(payload for name, payload in ctx.events.emitted if name == "session_close_flush_end")
    )
    assert end_payload["error"] is not None
    assert "RuntimeError" in end_payload["error"]
    assert "kaboom" in end_payload["error"]
    # Turn kind restored even on error.
    assert ctx._current_turn_kind == "previous"


def test_handler_prompt_carries_sender_name_and_silent_contract() -> None:
    """Smoke-check the literal prompt: includes sender name, the idle
    minutes, and the no-send_message instruction."""
    ctx = _StubCtx()
    event = _make_event(sender_name="Jason", idle_minutes=14)

    asyncio.run(handle_idle(ctx, event))

    prompt = ctx.run_turn_calls[0]["prompt"]
    assert "Jason" in prompt
    assert "14m" in prompt
    assert "append_note" in prompt
    assert "No send_message" in prompt


# ---------------------------------------------------------------------------
# Integration-shaped: IdleEvent through the source's handle() method


def test_source_handle_dispatches_to_handle_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """:meth:`IdleFlushSource.handle` is the registry's hook — confirm
    it routes through :func:`handle_idle` with the same ctx + event."""

    async def runner() -> None:
        ctx = _StubCtx()
        source = IdleFlushSource()
        event = _make_event(sender_name="Katie", idle_minutes=11)

        await source.handle(ctx, event)

        # The handler ran one silent turn — implies the dispatch worked.
        assert len(ctx.run_turn_calls) == 1
        assert ctx.run_turn_calls[0]["silent"] is True
        # Start/end events both emitted with the right sender name.
        emitted_kinds = [name for name, _ in ctx.events.emitted]
        assert emitted_kinds == [
            "session_close_flush_start",
            "session_close_flush_end",
        ]
        start = next(p for n, p in ctx.events.emitted if n == "session_close_flush_start")
        assert start["sender_name"] == "Katie"

    asyncio.run(runner())
