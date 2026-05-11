"""Tests for the BackgroundTaskCompleteEvent handler.

Verifies the handler restores the originating channel/principal,
invokes a turn through the daemon's ``_run_turn`` proxy, and emits
the dispatch + turn-end events. A stub ``ctx`` mocks just enough of
:class:`DaemonContext` to drive the handler without booting a full
daemon.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from alice_speaking._dispatch import handle_background_task_complete
from alice_speaking.internal import BackgroundTaskCompleteEvent
from alice_speaking.transports import ChannelRef


# ---------------------------------------------------------------------------
# Tiny stub ctx — only the attributes the handler reads/writes


@dataclass
class _StubEvents:
    emitted: list[tuple[str, dict]]

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


class _StubCtx:
    def __init__(self) -> None:
        self.events = _StubEvents(emitted=[])
        self._current_turn_kind: Optional[str] = "previous"
        self._current_reply_channel: Optional[ChannelRef] = None
        self._current_principal_display_name: Optional[str] = "previous-user"
        self._turn_did_send: bool = False
        self.run_turn_calls: list[dict] = []
        self.run_turn_should_raise: Optional[Exception] = None
        # Snapshot what's set when _run_turn was called — the handler
        # should restore originating channel + principal BEFORE running
        # the turn.
        self.channel_at_run_turn: Optional[ChannelRef] = None
        self.principal_at_run_turn: Optional[str] = None
        self.kind_at_run_turn: Optional[str] = None

    async def _run_turn(self, prompt: str, *, turn_id: str, outbound_recipient):
        self.run_turn_calls.append(
            {
                "prompt": prompt,
                "turn_id": turn_id,
                "outbound_recipient": outbound_recipient,
            }
        )
        self.channel_at_run_turn = self._current_reply_channel
        self.principal_at_run_turn = self._current_principal_display_name
        self.kind_at_run_turn = self._current_turn_kind
        if self.run_turn_should_raise is not None:
            raise self.run_turn_should_raise


# ---------------------------------------------------------------------------
# Tests


def _make_event(**overrides) -> BackgroundTaskCompleteEvent:
    defaults = dict(
        handle="bg-abc",
        description="research X",
        result_text="here is what i found about X",
        is_error=False,
        channel=ChannelRef(transport="signal", address="+15551234567", durable=True),
        principal_name="Jason",
    )
    defaults.update(overrides)
    return BackgroundTaskCompleteEvent(**defaults)


def test_handler_runs_turn_with_restored_channel_and_principal() -> None:
    ctx = _StubCtx()
    event = _make_event()

    asyncio.run(handle_background_task_complete(ctx, event))

    assert len(ctx.run_turn_calls) == 1
    # The originating channel + principal must be live during _run_turn
    # so send_message(recipient='self') routes back to Jason on Signal.
    assert ctx.channel_at_run_turn == event.channel
    assert ctx.principal_at_run_turn == "Jason"
    assert ctx.kind_at_run_turn == "signal"  # mirrors the channel


def test_handler_restores_previous_state_after_turn() -> None:
    ctx = _StubCtx()
    event = _make_event()

    asyncio.run(handle_background_task_complete(ctx, event))

    # Outside of the turn, the prior state must be restored. Critical
    # because the handler runs under the daemon's serial consumer and
    # the next turn's setup expects no dangling state from this one.
    assert ctx._current_turn_kind == "previous"
    assert ctx._current_reply_channel is None
    assert ctx._current_principal_display_name == "previous-user"


def test_handler_includes_handle_description_and_result_in_prompt() -> None:
    ctx = _StubCtx()
    event = _make_event(
        handle="bg-xyz789",
        description="check the deploy",
        result_text="ci pipeline is green",
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    prompt = ctx.run_turn_calls[0]["prompt"]
    assert "bg-xyz789" in prompt
    assert "check the deploy" in prompt
    assert "ci pipeline is green" in prompt
    assert "Jason" in prompt  # principal name surfaces


def test_handler_frames_failures_differently() -> None:
    ctx = _StubCtx()
    event = _make_event(
        is_error=True,
        result_text="Sub-agent crashed: TimeoutError: …",
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    prompt = ctx.run_turn_calls[0]["prompt"]
    assert "failed" in prompt.lower()
    assert "TimeoutError" in prompt


def test_handler_handles_empty_result_text_gracefully() -> None:
    ctx = _StubCtx()
    event = _make_event(result_text="")

    asyncio.run(handle_background_task_complete(ctx, event))

    prompt = ctx.run_turn_calls[0]["prompt"]
    assert "(sub-agent returned no text)" in prompt


def test_handler_emits_dispatch_and_turn_end_events() -> None:
    ctx = _StubCtx()
    event = _make_event()

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_dispatch" in kinds
    assert "background_task_turn_end" in kinds


def test_handler_emits_turn_end_with_error_when_run_turn_raises() -> None:
    ctx = _StubCtx()
    ctx.run_turn_should_raise = RuntimeError("kernel boom")
    event = _make_event()

    asyncio.run(handle_background_task_complete(ctx, event))

    end_events = [
        kw for k, kw in ctx.events.emitted if k == "background_task_turn_end"
    ]
    assert len(end_events) == 1
    assert "RuntimeError" in (end_events[0].get("error") or "")
    # State still restored even on error.
    assert ctx._current_turn_kind == "previous"


def test_handler_with_no_channel_falls_through_safely() -> None:
    """Synthetic event from a surface-originating dispatch could have
    no channel. Handler should still run the turn without crashing —
    Alice just won't be able to reply via 'self'."""
    ctx = _StubCtx()
    event = _make_event(channel=None)

    asyncio.run(handle_background_task_complete(ctx, event))

    assert len(ctx.run_turn_calls) == 1
    assert ctx.run_turn_calls[0]["outbound_recipient"] is None
    # Channel stays None during the turn (nothing to restore).
    assert ctx.channel_at_run_turn is None
