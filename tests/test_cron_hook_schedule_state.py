"""Tests for ``SpeakingDaemon._record_cron_tool_result`` / its call from
``_posttooluse_hook`` — the wiring that mirrors ``CronCreate`` /
``CronDelete`` / ``ScheduleWakeup`` outcomes into
:mod:`alice_speaking.schedule_state` for the status-site Schedules
panel (issue #815).

Following the ``_StubDaemon`` + ``SpeakingDaemon.<method>.__get__``
binding pattern from ``test_mid_turn_injection.py`` and
``test_task_interception.py`` — no full daemon boot, just the bound
methods exercised with stub state.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from alice_speaking import daemon as daemon_module
from alice_speaking.daemon import SpeakingDaemon
from alice_speaking.transports.base import ChannelRef


# ---------------------------------------------------------------------------
# Shared stubs


class _StubEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


class _StubScheduleState:
    """Records every call ``_record_cron_tool_result`` makes.

    We assert on ``record_calls`` / ``forget_calls`` directly — zero
    filesystem touches, so the test is fast and deterministic.
    """

    def __init__(self, *, record_raises: BaseException | None = None) -> None:
        self.record_calls: list[dict[str, Any]] = []
        self.forget_calls: list[str] = []
        self._record_raises = record_raises

    def record_cron(self, **kwargs) -> None:
        self.record_calls.append(dict(kwargs))
        if self._record_raises is not None:
            raise self._record_raises

    def forget_cron(self, job_id: str) -> bool:
        self.forget_calls.append(str(job_id))
        return True


class _StubDaemon:
    """Minimal stand-in carrying the attributes the methods read.

    Bare-metal: just enough state for ``_record_cron_tool_result`` and
    the existing ``_posttooluse_hook`` mid-turn-inbox drain to coexist.
    Class-level attributes aliased from ``SpeakingDaemon`` so ``self.``
    lookups inside the bound methods route to the real implementations.
    """

    _channel_key = staticmethod(SpeakingDaemon._channel_key)
    _record_cron_tool_result = SpeakingDaemon._record_cron_tool_result

    def __init__(self) -> None:
        self.events = _StubEvents()
        self._mid_turn_inbox: dict[str, list[tuple[str, Any]]] = {}
        self._current_reply_channel: Optional[ChannelRef] = None
        self._current_principal_display_name: Optional[str] = None
        self._current_turn_replied: bool = False


def _bind(stub: _StubDaemon):
    return {
        "record": SpeakingDaemon._record_cron_tool_result.__get__(
            stub, _StubDaemon
        ),
        "post_hook": SpeakingDaemon._posttooluse_hook.__get__(
            stub, _StubDaemon
        ),
    }


def _install_stub_schedule_state(
    monkeypatch, *, record_raises: BaseException | None = None
) -> _StubScheduleState:
    stub = _StubScheduleState(record_raises=record_raises)
    monkeypatch.setattr(daemon_module, "schedule_state", stub)
    return stub


def _hook_input(
    tool_name: str,
    *,
    tool_input: dict | None = None,
    tool_response: str | None = None,
    tool_use_id: str = "toolu_test_001",
) -> dict:
    """PostToolUseHookInput-shaped dict. ``tool_response`` omitted when
    the branch under test shouldn't read it."""
    out: dict = {
        "session_id": "sess-test",
        "transcript_path": "/dev/null",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": dict(tool_input or {}),
        "tool_use_id": tool_use_id,
    }
    if tool_response is not None:
        out["tool_response"] = tool_response
    return out


# ---------------------------------------------------------------------------
# CronCreate — recurring + plain phrasing both parse, garbage is a no-op


def test_croncreate_recurring_response_records_id_and_cron(monkeypatch) -> None:
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    fns["record"](
        _hook_input(
            "CronCreate",
            tool_input={
                "cron": "*/5 * * * *",
                "prompt": "SECRET_LEAK_CANARY babysit PRs",
                "recurring": True,
            },
            tool_response=(
                "Scheduled recurring job 1f5027d8 (Every 5 minutes). "
                "Next run: Nov 7 10:10am. Session-only (not written to "
                "disk, dies when Claude exits). Auto-expires after 7 "
                "days. Use CronDelete to cancel sooner."
            ),
        )
    )

    assert sched.forget_calls == []
    assert len(sched.record_calls) == 1
    call = sched.record_calls[0]
    assert call == {"id": "1f5027d8", "cron": "*/5 * * * *"}
    # Guard: description / next_run_at deliberately unset — do NOT let a
    # future contributor "helpfully" parse the "Next run:" substring,
    # and do NOT let the prompt leak as a description.
    assert "description" not in call
    assert "next_run_at" not in call
    assert "SECRET_LEAK_CANARY" not in repr(call)


def test_croncreate_plain_job_response_records_id_and_cron(monkeypatch) -> None:
    """One-shot jobs render as "Scheduled job <id>" (no "recurring")."""
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    fns["record"](
        _hook_input(
            "CronCreate",
            tool_input={"cron": "0 9 * * 1", "prompt": "weekly digest"},
            tool_response="Scheduled job abc123 (one-shot in 1 minute).",
        )
    )

    assert sched.forget_calls == []
    assert len(sched.record_calls) == 1
    assert sched.record_calls[0] == {"id": "abc123", "cron": "0 9 * * 1"}
    assert "description" not in sched.record_calls[0]
    assert "next_run_at" not in sched.record_calls[0]


def test_croncreate_unrecognized_response_is_noop(monkeypatch) -> None:
    """Defense against future CLI phrasing changes: no match → no write."""
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    fns["record"](
        _hook_input(
            "CronCreate",
            tool_input={"cron": "*/5 * * * *", "prompt": "x"},
            tool_response="Oops: the CLI changed its mind about wording.",
        )
    )

    assert sched.record_calls == []
    assert sched.forget_calls == []


def test_croncreate_missing_response_key_is_noop(monkeypatch) -> None:
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    # No tool_response key at all — defensive.
    fns["record"](
        _hook_input("CronCreate", tool_input={"cron": "*/5 * * * *"})
    )
    assert sched.record_calls == []
    assert sched.forget_calls == []


# ---------------------------------------------------------------------------
# CronDelete — id comes from tool_input, tool_response never read


def test_crondelete_forgets_by_tool_input_id(monkeypatch) -> None:
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    # tool_response deliberately omitted — the branch must not read it.
    fns["record"](
        _hook_input("CronDelete", tool_input={"id": "1f5027d8"})
    )

    assert sched.record_calls == []
    assert sched.forget_calls == ["1f5027d8"]


def test_crondelete_blank_id_is_noop(monkeypatch) -> None:
    """Blank id → skip the forget call; don't let a schema-violating
    input corrupt state."""
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    fns["record"](_hook_input("CronDelete", tool_input={"id": ""}))

    assert sched.record_calls == []
    assert sched.forget_calls == []


# ---------------------------------------------------------------------------
# ScheduleWakeup — synthesized id + reason-as-description, never prompt


def test_schedulewakeup_records_synthesized_id_and_fields(monkeypatch) -> None:
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    before = datetime.now(tz=timezone.utc)
    fns["record"](
        _hook_input(
            "ScheduleWakeup",
            tool_input={
                "delaySeconds": 300,
                "reason": "nudge the PR follow-up thread",
                "prompt": "SECRET_LEAK_CANARY internal directive text",
            },
            tool_use_id="toolu_wake_xyz",
        )
    )
    after = datetime.now(tz=timezone.utc)

    assert sched.forget_calls == []
    assert len(sched.record_calls) == 1
    call = sched.record_calls[0]
    assert call["id"] == "wakeup-toolu_wake_xyz"
    assert call["cron"] == "once"
    assert call["description"] == "nudge the PR follow-up thread"
    # next_run_at was computed from now + delaySeconds — parse it and
    # confirm it lands in [before+delay, after+delay] with a bit of
    # slack for the sub-second truncation performed by isoformat.
    parsed = datetime.fromisoformat(call["next_run_at"])
    assert parsed.tzinfo is not None
    delta = parsed.timestamp() - before.timestamp()
    assert 299 <= delta <= (after - before).total_seconds() + 301

    # Prompt field is NEVER read. Confirm it leaks into neither the
    # kwargs nor the stringified call record.
    assert "prompt" not in call
    assert "SECRET_LEAK_CANARY" not in repr(call)


def test_schedulewakeup_zero_delay_still_records(monkeypatch) -> None:
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()
    fns = _bind(stub)

    fns["record"](
        _hook_input(
            "ScheduleWakeup",
            tool_input={"delaySeconds": 0, "reason": "immediate"},
            tool_use_id="toolu_wake_0",
        )
    )

    assert len(sched.record_calls) == 1
    call = sched.record_calls[0]
    assert call["id"] == "wakeup-toolu_wake_0"
    assert call["cron"] == "once"
    assert call["description"] == "immediate"
    # next_run_at parses as a valid ISO-8601 string.
    datetime.fromisoformat(call["next_run_at"])


# ---------------------------------------------------------------------------
# Regression guards


def test_unrelated_tool_is_noop_and_preserves_posttooluse_return(
    monkeypatch,
) -> None:
    """Tools outside the cron set must not touch schedule_state *and*
    must not short-circuit the existing mid-turn-inbox branch."""
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()  # no _current_reply_channel set
    fns = _bind(stub)

    result = asyncio.run(
        fns["post_hook"](
            _hook_input(
                "Bash",
                tool_input={"command": "ls"},
                tool_response="file1\nfile2\n",
            ),
            "toolu_1",
            None,
        )
    )

    # With no reply channel, the existing hook returns {}. That branch
    # must still fire unchanged.
    assert result == {}
    # schedule_state not touched for Bash.
    assert sched.record_calls == []
    assert sched.forget_calls == []


def test_record_cron_exception_is_swallowed_and_hook_returns_normally(
    monkeypatch,
) -> None:
    """If schedule_state misbehaves (e.g. filesystem error), the hook
    must still return the mid-turn-inbox value and not blow up the turn."""
    sched = _install_stub_schedule_state(
        monkeypatch, record_raises=RuntimeError("disk full")
    )
    stub = _StubDaemon()  # no reply channel → hook returns {}
    fns = _bind(stub)

    result = asyncio.run(
        fns["post_hook"](
            _hook_input(
                "CronCreate",
                tool_input={"cron": "*/5 * * * *"},
                tool_response="Scheduled recurring job explode (details).",
            ),
            "toolu_1",
            None,
        )
    )

    # The mirror attempt was made (so we know the swallow actually ran
    # over a raising record_cron)...
    assert len(sched.record_calls) == 1
    # ...and the hook's return is the mid-turn-inbox branch's normal {}.
    assert result == {}


def test_posttooluse_hook_calls_mirror_before_early_return(
    monkeypatch,
) -> None:
    """The mirror must fire even when ``_current_reply_channel`` is
    ``None`` (which triggers the existing early return). The two
    concerns are independent."""
    sched = _install_stub_schedule_state(monkeypatch)
    stub = _StubDaemon()  # _current_reply_channel left as None
    fns = _bind(stub)

    result = asyncio.run(
        fns["post_hook"](
            _hook_input(
                "CronDelete",
                tool_input={"id": "abc123"},
            ),
            "toolu_1",
            None,
        )
    )

    assert sched.forget_calls == ["abc123"]
    assert result == {}


# ---------------------------------------------------------------------------
# Filesystem integration — round-trip through the real reader


def test_round_trip_through_status_site_reader(monkeypatch, tmp_path) -> None:
    """End-to-end: CronCreate then CronDelete (same id) through the
    bound hook, no monkeypatch on schedule_state. The on-disk JSON
    must round-trip through ``status_site.sources.schedules.read_jobs``
    to an empty ``jobs`` list — upsert-then-remove nets to nothing.
    """
    # Both modules read these env vars via the same convention — see
    # ``alice_speaking.schedule_state.active_crons_path`` and
    # ``status_site.sources.schedules.active_crons_path``.
    path = tmp_path / "active-crons.json"
    monkeypatch.setenv("ACTIVE_CRONS_PATH", str(path))

    # Important: the writer side caches a module-level global pointing
    # at the default path. Reset it so our env override takes effect.
    from alice_speaking import schedule_state as real_ss

    monkeypatch.setattr(real_ss, "_GLOBAL_STATE", None)

    from status_site.sources import schedules as reader

    stub = _StubDaemon()
    fns = _bind(stub)

    fns["record"](
        _hook_input(
            "CronCreate",
            tool_input={"cron": "*/5 * * * *", "prompt": "noise"},
            tool_response="Scheduled recurring job roundtrip-1 (details).",
        )
    )
    # After CronCreate: the row is present on disk.
    assert path.exists()
    mid = json.loads(path.read_text())
    assert [j["id"] for j in mid["jobs"]] == ["roundtrip-1"]
    assert reader.read_jobs(path)[0]["id"] == "roundtrip-1"

    fns["record"](
        _hook_input(
            "CronDelete",
            tool_input={"id": "roundtrip-1"},
        )
    )

    # After CronDelete: file still exists but jobs list is empty.
    end = json.loads(path.read_text())
    assert end["jobs"] == []
    assert reader.read_jobs(path) == []
