"""Phase 6b of plan 01: CompactionTrigger unit tests.

The trigger replaces the bare ``self._compaction_pending`` flag and
the inline ``_run_compaction`` method on :class:`SpeakingDaemon`.
Three contracts:

1. Pending state — ``arm()`` flips pending=True; ``should_run(event)``
   returns the pending flag (deferral hook is a TODO until
   SessionDepthSignal lands).
2. Run lifecycle — ``run(ctx)`` invokes ``ctx._run_turn`` with the
   compaction prompt, writes the summary to ``ctx._summary_path``,
   rolls the session (clears ``ctx.session_id`` and the on-disk
   session-state file), clears the pending flag, and primes the
   bootstrap preamble.
3. Failure path — when ``_run_turn`` raises, the trigger emits an
   error event, clears pending (don't get stuck looping), and
   leaves the session intact.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

from alice_speaking.compaction import CompactionTrigger


# ---------------------------------------------------------------------------
# Pending semantics


def test_new_trigger_is_idle():
    t = CompactionTrigger()
    assert t.pending() is False
    assert t.should_run(event=None) is False


def test_arm_marks_pending():
    t = CompactionTrigger()
    t.arm()
    assert t.pending() is True
    assert t.should_run(event=None) is True


def test_should_run_currently_returns_pending_flag_for_any_event():
    """Until the SessionDepthSignal landing pad ships, should_run is
    just ``return pending``. This test pins that so a future change
    to add deferral has to update both call sites."""
    t = CompactionTrigger()
    t.arm()
    # All event types route through the same answer today.
    for event in (object(), {"shape": "anything"}, None):
        assert t.should_run(event) is True


# ---------------------------------------------------------------------------
# Run lifecycle


def _make_ctx(tmp_path: pathlib.Path, *, run_turn_summary: str = "summary"):
    """Build a stub ``ctx`` exposing exactly the surfaces
    :meth:`CompactionTrigger.run` reaches."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    summary_path = state_dir / "context-summary.md"
    session_path = state_dir / "session.json"
    session_path.write_text(json.dumps({"session_id": "to-be-rolled"}))

    events: list[tuple[str, dict]] = []

    class _Events:
        def emit(self, name: str, **fields) -> None:
            events.append((name, fields))

    primed = []

    class _Ctx:
        session_id: Any = "to-be-rolled"
        _summary_path = summary_path
        _session_path = session_path
        events = _Events()

        async def _run_turn(self, prompt, *, turn_id, outbound_recipient, silent):
            return run_turn_summary

        def _prime_bootstrap_preamble(self):
            primed.append(True)

    return _Ctx(), events, primed, summary_path, session_path


def test_run_writes_summary_rolls_session_and_primes_preamble(tmp_path):
    t = CompactionTrigger()
    t.arm()
    ctx, events, primed, summary_path, session_path = _make_ctx(
        tmp_path, run_turn_summary="rolled summary"
    )
    asyncio.run(t.run(ctx))

    assert t.pending() is False
    assert ctx.session_id is None
    assert not session_path.exists()
    assert summary_path.is_file()
    assert "rolled summary" in summary_path.read_text()
    assert primed == [True]
    # Two terminal events emitted.
    names = [name for name, _ in events]
    assert "context_compaction_start" in names
    assert "context_compaction" in names
    assert "session_roll" in names


def test_run_handles_run_turn_failure_clears_pending(tmp_path):
    t = CompactionTrigger()
    t.arm()
    ctx, events, primed, summary_path, session_path = _make_ctx(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("kernel down")

    ctx._run_turn = boom

    asyncio.run(t.run(ctx))

    # Don't get stuck looping — pending cleared.
    assert t.pending() is False
    # Session NOT rolled when the compaction turn itself fails.
    assert ctx.session_id == "to-be-rolled"
    assert session_path.exists()
    # Error event emitted with the exception class name.
    error_events = [ev for ev in events if ev[0] == "context_compaction_error"]
    assert len(error_events) == 1
    assert "RuntimeError" in error_events[0][1]["error"]
    # Bootstrap preamble NOT primed (no successful summary to inject).
    assert primed == []


def test_run_handles_empty_summary_with_placeholder(tmp_path):
    """Edge case: kernel returns no text. Trigger writes a
    placeholder so the on-disk summary file is never empty (the
    Layer-2 bootstrap reader uses non-empty as 'summary present')."""
    t = CompactionTrigger()
    t.arm()
    ctx, events, primed, summary_path, session_path = _make_ctx(
        tmp_path, run_turn_summary=""
    )
    asyncio.run(t.run(ctx))

    assert summary_path.is_file()
    body = summary_path.read_text()
    assert "compaction produced no summary" in body
    # Session still rolls — empty summary doesn't gate the roll.
    assert ctx.session_id is None
