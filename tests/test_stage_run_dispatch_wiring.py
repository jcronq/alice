"""Integration tests for the slice-timeline dispatch wire-up.

Exercises the completion-side (``handle_background_task_complete``)
POST to ``/api/stage-run``. The started-side (in
``_dispatch_subagent``) is exercised indirectly by asserting the
event carries ``item_slug`` / ``stage`` / ``started_at`` — but we
don't boot the full daemon here. The unit-level inference tests
live in ``test_stage_run_client.py``; this file is about the
``BackgroundTaskCompleteEvent`` plumbing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from alice_speaking import _dispatch as _dispatch_module
from alice_speaking._dispatch import handle_background_task_complete
from alice_speaking.internal import BackgroundTaskCompleteEvent
from alice_speaking.transports import ChannelRef


# ---------------------------------------------------------------------------
# Tiny stub ctx — mirrors tests/test_background_task_completion.py


@dataclass
class _StubEvents:
    emitted: list[tuple[str, dict]]

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


class _StubCtx:
    def __init__(self) -> None:
        self.events = _StubEvents(emitted=[])
        self._current_turn_kind: Optional[str] = None
        self._current_reply_channel: Optional[ChannelRef] = None
        self._current_principal_display_name: Optional[str] = None
        self._turn_did_send: bool = False

    async def _run_turn(self, prompt: str, *, turn_id: str, outbound_recipient):
        return None


def _make_event(**overrides) -> BackgroundTaskCompleteEvent:
    defaults = dict(
        handle="bg-abc",
        description="build slice X",
        result_text="done",
        is_error=False,
        channel=ChannelRef(transport="signal", address="+15551234567", durable=True),
        principal_name="Jason",
        item_slug="task-0042",
        stage="build",
        started_at="2026-11-17T09:00:00-05:00",
    )
    defaults.update(overrides)
    return BackgroundTaskCompleteEvent(**defaults)


class _CapturedCall:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kw) -> bool:
        self.calls.append(kw)
        return True


# ---------------------------------------------------------------------------
# Completion handler calls post_stage_run with the terminal status


def test_completion_emits_success_stage_run(monkeypatch) -> None:
    captured = _CapturedCall()
    monkeypatch.setattr(
        _dispatch_module.stage_run_client_module,
        "post_stage_run",
        captured,
    )
    ctx = _StubCtx()
    event = _make_event()

    asyncio.run(handle_background_task_complete(ctx, event))

    assert len(captured.calls) == 1
    call = captured.calls[0]
    assert call["item_slug"] == "task-0042"
    assert call["stage"] == "build"
    assert call["status"] == "success"
    assert call["task_id"] == "bg-abc"
    # The ended_at string is "now" — assert it's present + ISO-shaped.
    assert call["ended_at"] and ":" in call["ended_at"]
    # started_at threaded through from the event so the tooltip can
    # show both anchors.
    assert call["started_at"] == "2026-11-17T09:00:00-05:00"


def test_completion_emits_failed_stage_run_on_error(monkeypatch) -> None:
    captured = _CapturedCall()
    monkeypatch.setattr(
        _dispatch_module.stage_run_client_module,
        "post_stage_run",
        captured,
    )
    ctx = _StubCtx()
    event = _make_event(
        is_error=True, result_text="Sub-agent crashed: TimeoutError"
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    assert len(captured.calls) == 1
    assert captured.calls[0]["status"] == "failed"


def test_completion_skips_stage_run_when_no_item_slug(monkeypatch) -> None:
    captured = _CapturedCall()
    monkeypatch.setattr(
        _dispatch_module.stage_run_client_module,
        "post_stage_run",
        captured,
    )
    ctx = _StubCtx()
    # No item context on the event — the started-at was skipped at
    # dispatch time, so the ended-at MUST stay silent too.
    event = _make_event(item_slug=None, stage=None, started_at="")

    asyncio.run(handle_background_task_complete(ctx, event))

    assert captured.calls == []


def test_completion_soft_fails_if_stage_run_raises(monkeypatch) -> None:
    def _boom(**kw):  # noqa: ARG001
        raise RuntimeError("endpoint on fire")

    monkeypatch.setattr(
        _dispatch_module.stage_run_client_module,
        "post_stage_run",
        _boom,
    )
    ctx = _StubCtx()
    event = _make_event()

    # Must NOT raise — the dispatch completion turn is correctness-
    # critical, the stage-run POST is observability.
    asyncio.run(handle_background_task_complete(ctx, event))
