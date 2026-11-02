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


# ---------------------------------------------------------------------------
# REQUIRED-REPORT parse + pending-verify path (self-directed-agents item 1)


class _CfgWithMindDir:
    def __init__(self, mind_dir) -> None:
        self.mind_dir = mind_dir


def _ok_report_json() -> str:
    return (
        '{"kind":"code","branch":"feat/x",'
        '"commit_sha":"0123456789abcdef0123456789abcdef01234567",'
        '"pr_url":"https://github.com/jcronq/alice/pull/999",'
        '"baseline_suite":{"passed":10,"failed":0,"errors":0,"skipped":0},'
        '"full_suite_after":{"passed":11,"failed":0,"errors":0,"skipped":0},'
        '"live_verified":"n/a","amendments_addressed":[],'
        '"unresolved":[],"lessons":[]}'
    )


def test_well_formed_report_does_not_emit_pending_verify(tmp_path) -> None:
    ctx = _StubCtx()
    ctx.cfg = _CfgWithMindDir(tmp_path)
    event = _make_event(
        result_text=f"Summary.\n\n```json\n{_ok_report_json()}\n```",
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_pending_verify" not in kinds
    assert "REPORT CHECK FAILED" not in ctx.run_turn_calls[0]["prompt"]
    assert not (tmp_path / "inner" / "notes").exists()


def test_missing_report_writes_pending_verify_note(tmp_path) -> None:
    ctx = _StubCtx()
    ctx.cfg = _CfgWithMindDir(tmp_path)
    event = _make_event(
        result_text="all done, no fences anywhere here",
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_pending_verify" in kinds
    notes_dir = tmp_path / "inner" / "notes"
    assert notes_dir.is_dir()
    notes = list(notes_dir.glob("*.md"))
    assert len(notes) == 1
    assert "pending-verify" in notes[0].read_text()
    # Delivery prompt tells Alice to triage.
    assert "REPORT CHECK FAILED" in ctx.run_turn_calls[0]["prompt"]


def test_invalid_report_writes_pending_verify_note(tmp_path) -> None:
    ctx = _StubCtx()
    ctx.cfg = _CfgWithMindDir(tmp_path)
    # Code kind without commit_sha → should trip validation.
    bad = (
        '{"kind":"code","branch":"feat/x",'
        '"full_suite_after":{"passed":1,"failed":0,"errors":0,"skipped":0}}'
    )
    event = _make_event(
        result_text=f"Here you go.\n\n```json\n{bad}\n```",
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_pending_verify" in kinds
    notes = list((tmp_path / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "commit_sha" in notes[0].read_text()


def test_valid_report_but_done_claim_without_evidence_in_prose(tmp_path) -> None:
    """Secondary prose-level check (self-directed-agents item 3):
    even when the structured report is well-formed, a worker whose
    prose claims completion without naming a commit / PR / tests
    still trips a pending-verify. Catches the "I say done, structured
    report shows null" edge where the two tiers would otherwise
    disagree."""
    ctx = _StubCtx()
    ctx.cfg = _CfgWithMindDir(tmp_path)
    # Research kind so the structured check passes; prose claims done
    # with no commit / PR / test counts.
    research = (
        '{"kind":"research",'
        '"live_verified":"cortex-memory/research/foo.md"}'
    )
    body = (
        "Done! I looked into this and here's what I found.\n\n"
        f"```json\n{research}\n```"
    )
    event = _make_event(result_text=body)

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_pending_verify" in kinds
    notes = list((tmp_path / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "prose-level evidence check" in text


def test_valid_report_with_evidence_in_prose_passes(tmp_path) -> None:
    """The counter-example: valid structured report AND prose names
    the commit / PR / tests. No pending-verify."""
    ctx = _StubCtx()
    ctx.cfg = _CfgWithMindDir(tmp_path)
    body = (
        "Done — merged in PR #42 with 123 tests passing.\n\n"
        f"```json\n{_ok_report_json()}\n```"
    )
    event = _make_event(result_text=body)

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_pending_verify" not in kinds


def test_error_event_skips_pending_verify(tmp_path) -> None:
    # If the sub-agent itself crashed, we don't double-penalise by
    # also dropping a pending-verify — the error framing in the
    # delivery prompt is enough signal for Alice.
    ctx = _StubCtx()
    ctx.cfg = _CfgWithMindDir(tmp_path)
    event = _make_event(
        is_error=True,
        result_text="Sub-agent crashed: ValueError: boom",
    )

    asyncio.run(handle_background_task_complete(ctx, event))

    kinds = [k for k, _ in ctx.events.emitted]
    assert "background_task_pending_verify" not in kinds
    assert not (tmp_path / "inner" / "notes").exists()
