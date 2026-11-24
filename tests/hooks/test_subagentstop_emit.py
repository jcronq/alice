"""Unit tests for ``sandbox/claude-hooks/subagentstop-emit.py``.

Every test invokes the script with a captured SubagentStop payload fixture
and asserts the resulting ``alice-subagent-stop.jsonl`` row matches the
schema in the design brief §2.1.
"""
from __future__ import annotations

import json
import time


def _write_transcript(tmp_path, task_id: str | None = None):
    """Write a Claude Code subagent transcript file with the auto-task sentinel.

    The ``pretooluse-agent-task.py`` hook injects the sentinel into the
    subagent's context via ``hookSpecificOutput.additionalContext``; it
    surfaces as plain text in the transcript, which is what we scan for.
    """
    transcript = tmp_path / "subagent-transcript.jsonl"
    body = {"type": "user", "content": "Do the thing."}
    if task_id:
        body["additionalContext"] = (
            f"[auto-task] This Agent dispatch is being tracked as {task_id} in the SM v2 store."
        )
    transcript.write_text(json.dumps(body) + "\n", encoding="utf-8")
    return transcript


def test_captured_payload_writes_one_wellformed_stop_row(hook_runner, start_log_writer, tmp_path):
    """Design §2.1 acceptance: one SubagentStop payload → one wellformed row."""
    start_ts = time.time() - 42.0
    start_log_writer(
        [
            {
                "ts": start_ts,
                "task_id": "task-1025",
                "tool_call_id": "toolu_01KsXak9...",
                "session_id": "parent-session-id",
                "subagent_type": "general-purpose",
                "description": "Bug-tile approve ...",
                "title": "Bug-tile approve ...",
                "tags": ["auto-registered", "status-site"],
            }
        ]
    )
    transcript = _write_transcript(tmp_path, task_id="task-1025")

    payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "subagent-session-id",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "reason": "end_turn",
    }

    rows, _stderr = hook_runner("subagentstop-emit.py", payload)
    assert len(rows) == 1
    row = rows[0]
    # Required schema fields per design §2.1.
    for key in (
        "ts",
        "event",
        "tool_call_id",
        "task_id",
        "session_id",
        "duration_ms",
        "exit_status",
        "reaped_reason",
    ):
        assert key in row, f"missing field {key!r}: {row}"
    assert row["event"] == "subagent_stop"
    assert row["tool_call_id"] == "toolu_01KsXak9..."
    assert row["task_id"] == "task-1025"
    # Subagent's own session, not the parent's.
    assert row["session_id"] == "subagent-session-id"
    assert row["exit_status"] == "ok"
    assert row["reaped_reason"] is None
    # duration_ms derives from start row's ts; should be a positive int near 42s.
    assert isinstance(row["duration_ms"], int)
    assert 40_000 <= row["duration_ms"] <= 60_000


def test_error_reason_maps_to_error_exit_status(hook_runner, start_log_writer, tmp_path):
    """A reason mentioning ``error`` becomes ``exit_status=error``."""
    start_log_writer(
        [
            {
                "ts": time.time() - 5,
                "task_id": "task-9001",
                "tool_call_id": "toolu_err",
                "session_id": "parent",
            }
        ]
    )
    transcript = _write_transcript(tmp_path, task_id="task-9001")
    rows, _ = hook_runner(
        "subagentstop-emit.py",
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sub",
            "transcript_path": str(transcript),
            "reason": "error: subagent raised",
        },
    )
    assert rows[0]["exit_status"] == "error"


def test_no_matching_start_row_writes_null_tool_call_id(hook_runner, tmp_path):
    """Payload with no recoverable start row still writes an audit row."""
    transcript = _write_transcript(tmp_path, task_id=None)
    rows, _ = hook_runner(
        "subagentstop-emit.py",
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sub-unknown",
            "transcript_path": str(transcript),
        },
    )
    assert len(rows) == 1
    assert rows[0]["event"] == "subagent_stop"
    assert rows[0]["tool_call_id"] is None
    assert rows[0]["task_id"] is None
    # No start row → no duration we can compute.
    assert rows[0]["duration_ms"] is None


def test_payload_tool_use_id_wins_over_transcript_scan(hook_runner, start_log_writer, tmp_path):
    """If Anthropic ever adds ``tool_use_id`` to the payload, prefer it."""
    start_log_writer(
        [
            {
                "ts": time.time() - 1,
                "task_id": "task-direct",
                "tool_call_id": "toolu_direct",
                "session_id": "parent",
            }
        ]
    )
    # Transcript carries a DIFFERENT task_id — payload should win via tool_use_id.
    transcript = _write_transcript(tmp_path, task_id="task-wrong-from-transcript")
    rows, _ = hook_runner(
        "subagentstop-emit.py",
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sub",
            "transcript_path": str(transcript),
            "tool_use_id": "toolu_direct",
        },
    )
    assert rows[0]["tool_call_id"] == "toolu_direct"
    assert rows[0]["task_id"] == "task-direct"


def test_malformed_stdin_is_nonblocking(hook_runner):
    """Design §2.1: hook must never block the dispatcher. Exit 0 on bad input."""
    rows, _ = hook_runner("subagentstop-emit.py", "<not-json>")
    assert rows == []
