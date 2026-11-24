#!/usr/bin/env python3
"""SubagentStop hook: emit a deterministic stop row for each subagent exit.

Reads Claude Code's SubagentStop payload from stdin and appends a single JSON
row to ``/state/worker/alice-subagent-stop.jsonl``. Row shape matches the
design brief at
``cortex-memory/designs/2026-11-24-subagent-driven-running-now.md`` §2.1:

    {
      "ts": <float, now>,
      "event": "subagent_stop",
      "tool_call_id": <str or null>,
      "task_id":     <str or null>,
      "session_id":  <subagent session id>,
      "duration_ms": <int or null>,
      "exit_status": "ok" | "error" | "timeout" | "reaped",
      "reaped_reason": null
    }

Why both ``exit_status`` and ``reaped_reason``: a real SubagentStop hook firing
means the subagent returned normally (clean exit). ``reaped_reason`` is left
null here; the orphan reaper sets it when it synthesises a row for a crashed
or stuck subagent.

The hook joins back to the ``alice-task-dispatch.jsonl`` start row via the
``[auto-task] ... tracked as task-NNNN`` sentinel that the PreToolUse Agent
hook injects into the subagent's context — reading this from
``transcript_path`` recovers ``task_id``, which keys into the start log for
``tool_call_id`` and ``start_ts`` (→ ``duration_ms``).

Contract (design §2.1):
- Exit 0 unconditionally. Never block the dispatcher on a log-write failure.
- ``fcntl.flock`` on every write to the stop jsonl — concurrent SubagentStop
  firings from parallel subagents are legitimate.
- If no ``task_id`` can be inferred, still write a row with task_id=null;
  the audit trail is worth it even when the row can't join into the UI.
"""
from __future__ import annotations

import fcntl
import json
import re
import sys
import time
from pathlib import Path

STOP_LOG = Path("/state/worker/alice-subagent-stop.jsonl")
START_LOG = Path("/state/worker/alice-task-dispatch.jsonl")

# Injected by pretooluse-agent-task.py: "[auto-task] This Agent dispatch is
# being tracked as task-NNNN in the SM v2 store."
_TASK_RE = re.compile(r"tracked as (task-\d+)")


def _emit_err(msg: str) -> None:
    print(f"subagentstop-emit: {msg}", file=sys.stderr, flush=True)


def _scan_transcript_for_task_id(transcript_path: str | None) -> str | None:
    """Scan the subagent's own transcript for the auto-task sentinel.

    The sentinel lives in a ``hookSpecificOutput.additionalContext`` bubble the
    PreToolUse Agent hook emits — it surfaces inside the subagent's first user
    turn as context, so it's always in the transcript.
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    try:
        # Transcripts are small-to-medium JSONL; a single read is fine.
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _TASK_RE.search(text)
    return m.group(1) if m else None


def _lookup_start_row(task_id: str | None, tool_use_id: str | None) -> dict | None:
    """Reverse-scan the start log for a row matching task_id or tool_use_id.

    Walking backwards is cheap: the log is append-only, and the newest row is
    overwhelmingly the one we want. Fall back to a forward scan if the file
    isn't readable.
    """
    if not START_LOG.exists() or (not task_id and not tool_use_id):
        return None
    try:
        lines = START_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tool_use_id and row.get("tool_call_id") == tool_use_id:
            return row
        if task_id and row.get("task_id") == task_id:
            return row
    return None


def _classify_exit(payload: dict) -> str:
    """Map payload signals to ``exit_status``.

    Claude Code's SubagentStop payload currently carries ``reason`` and
    ``stop_hook_active``; richer error signals may land later. Today we treat
    any non-empty error field as ``error``, and the presence of a timeout
    indicator as ``timeout``. Default: ``ok``.
    """
    # Defensive: payload shape is Anthropic's, may evolve.
    if payload.get("error") or payload.get("is_error"):
        return "error"
    if payload.get("timeout") or payload.get("timed_out"):
        return "timeout"
    reason = (payload.get("reason") or "").lower()
    if "error" in reason or "fail" in reason:
        return "error"
    if "timeout" in reason:
        return "timeout"
    return "ok"


def _append_stop_row(row: dict) -> None:
    try:
        STOP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with STOP_LOG.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(row) + "\n")
            finally:
                # Explicit unlock before close for symmetry; close releases
                # the lock anyway but being explicit documents intent.
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        _emit_err(f"stop log write failed: {exc}")


def _build_row(payload: dict) -> dict:
    now = time.time()
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""

    # The payload sometimes carries the parent's tool_use_id directly; try
    # every common field name before falling back to transcript scan.
    tool_use_id = (
        payload.get("tool_use_id")
        or payload.get("tool_call_id")
        or payload.get("parent_tool_use_id")
        or payload.get("parentToolUseId")
        or None
    )
    task_id = _scan_transcript_for_task_id(transcript_path)
    start = _lookup_start_row(task_id, tool_use_id)
    if start:
        # Prefer the start row's tool_call_id as the authoritative primary key.
        tool_use_id = start.get("tool_call_id") or tool_use_id
        task_id = start.get("task_id") or task_id
        try:
            duration_ms = int((now - float(start.get("ts", now))) * 1000)
        except (TypeError, ValueError):
            duration_ms = None
    else:
        duration_ms = None

    return {
        "ts": now,
        "event": "subagent_stop",
        "tool_call_id": tool_use_id,
        "task_id": task_id,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "exit_status": _classify_exit(payload),
        "reaped_reason": None,
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        _emit_err(f"stdin parse failed: {exc}")
        return 0

    if not isinstance(payload, dict):
        _emit_err("stdin payload is not a JSON object")
        return 0

    row = _build_row(payload)
    _append_stop_row(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
