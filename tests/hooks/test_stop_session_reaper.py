"""Unit tests for ``sandbox/claude-hooks/stop-session-reaper.py``.

Design §2.2 acceptance test: 2 starts + 1 stop + Stop fires → 1 reap row for
the orphan.
"""
from __future__ import annotations

import json
import time


def test_two_starts_one_stop_yields_one_reap(hook_runner, start_log_writer):
    """Design §2.2 primary acceptance test."""
    session = "session-A"
    now = time.time()
    start_log_writer(
        [
            {
                "ts": now - 60,
                "task_id": "task-A",
                "tool_call_id": "toolu_A",
                "session_id": session,
            },
            {
                "ts": now - 30,
                "task_id": "task-B",
                "tool_call_id": "toolu_B",
                "session_id": session,
            },
        ]
    )
    # Seed the stop log with a clean exit for toolu_A only.
    with hook_runner.stop_log.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": now - 25,
                    "event": "subagent_stop",
                    "tool_call_id": "toolu_A",
                    "task_id": "task-A",
                    "session_id": session,
                    "duration_ms": 35_000,
                    "exit_status": "ok",
                    "reaped_reason": None,
                }
            )
            + "\n"
        )

    rows, _ = hook_runner(
        "stop-session-reaper.py",
        {"hook_event_name": "Stop", "session_id": session},
    )
    # One pre-existing clean stop + one new reap = two rows total.
    assert len(rows) == 2
    reap = next(r for r in rows if r["event"] == "subagent_reaped")
    assert reap["tool_call_id"] == "toolu_B"
    assert reap["task_id"] == "task-B"
    assert reap["session_id"] == session
    assert reap["reaped_reason"] == "session_end"
    assert reap["exit_status"] == "reaped"


def test_stop_reaper_skips_already_closed_orphans(hook_runner, start_log_writer):
    """Idempotency: running Stop twice must not double-reap."""
    session = "session-B"
    now = time.time()
    start_log_writer(
        [
            {
                "ts": now - 10,
                "task_id": "task-Z",
                "tool_call_id": "toolu_Z",
                "session_id": session,
            }
        ]
    )
    payload = {"hook_event_name": "Stop", "session_id": session}
    first_rows, _ = hook_runner("stop-session-reaper.py", payload)
    assert any(r["event"] == "subagent_reaped" for r in first_rows)
    before_count = len(first_rows)
    second_rows, _ = hook_runner("stop-session-reaper.py", payload)
    # Second call sees the previous reap row and writes nothing new.
    assert len(second_rows) == before_count


def test_stop_reaper_ignores_other_sessions(hook_runner, start_log_writer):
    """A Stop fires per-session: unrelated sessions' orphans are untouched."""
    now = time.time()
    start_log_writer(
        [
            {
                "ts": now - 5,
                "task_id": "task-mine",
                "tool_call_id": "toolu_mine",
                "session_id": "session-mine",
            },
            {
                "ts": now - 5,
                "task_id": "task-other",
                "tool_call_id": "toolu_other",
                "session_id": "session-other",
            },
        ]
    )
    rows, _ = hook_runner(
        "stop-session-reaper.py",
        {"hook_event_name": "Stop", "session_id": "session-mine"},
    )
    reaps = [r for r in rows if r["event"] == "subagent_reaped"]
    assert [r["tool_call_id"] for r in reaps] == ["toolu_mine"]


def test_missing_session_id_is_noop(hook_runner, start_log_writer):
    """Payload without session_id → do nothing (defensive)."""
    now = time.time()
    start_log_writer(
        [
            {
                "ts": now,
                "task_id": "task-x",
                "tool_call_id": "toolu_x",
                "session_id": "some-session",
            }
        ]
    )
    rows, _ = hook_runner(
        "stop-session-reaper.py",
        {"hook_event_name": "Stop"},  # no session_id
    )
    assert rows == []
