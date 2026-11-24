#!/usr/bin/env python3
"""Stop hook: session-boundary reaper for orphaned subagent starts.

Fires once per Claude Code session Stop. Scans the dispatch + stop jsonls for
start rows in this session that never got a matching stop row, and emits a
synthetic ``subagent_reaped`` row with ``reaped_reason=session_end`` for each.

This is the second net under the design's three-layer reap plan
(cortex-memory/designs/2026-11-24-subagent-driven-running-now.md §2.2):

1. ``subagentstop-emit.py`` handles clean subagent exits.
2. *This script* handles agents that outlive their SubagentStop (crash,
   killed mid-turn, SIGHUP on dispatcher exit).
3. ``src/hooks/orphan_reaper.py`` sweeps anything that survives session
   boundaries — host reboot, container restart, systemd restart.

Contract (design §2.2):
- Exit 0 unconditionally.
- Idempotent: if a row already has a stop (clean or reaped) for this
  tool_call_id, skip it. A later session Stop firing on the same session is
  harmless.
- ``fcntl.flock`` on writes.
"""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

START_LOG = Path("/state/worker/alice-task-dispatch.jsonl")
STOP_LOG = Path("/state/worker/alice-subagent-stop.jsonl")


def _emit_err(msg: str) -> None:
    print(f"stop-session-reaper: {msg}", file=sys.stderr, flush=True)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
    except OSError:
        return []


def _append_rows(rows: list[dict]) -> None:
    if not rows:
        return
    try:
        STOP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with STOP_LOG.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        _emit_err(f"stop log write failed: {exc}")


def _reap_orphans(session_id: str) -> list[dict]:
    """Return synthetic reap rows for this session's unclosed starts."""
    starts = _read_jsonl(START_LOG)
    stops = _read_jsonl(STOP_LOG)

    # Any tool_call_id that already has a stop row (clean or reaped) is closed.
    closed_ids: set[str] = set()
    for row in stops:
        tcid = row.get("tool_call_id")
        if tcid:
            closed_ids.add(tcid)

    now = time.time()
    reap_rows: list[dict] = []
    seen_in_session: set[str] = set()
    for row in starts:
        if row.get("session_id") != session_id:
            continue
        tcid = row.get("tool_call_id") or ""
        if not tcid:
            continue
        if tcid in closed_ids:
            continue
        if tcid in seen_in_session:
            continue
        seen_in_session.add(tcid)
        try:
            duration_ms = int((now - float(row.get("ts", now))) * 1000)
        except (TypeError, ValueError):
            duration_ms = None
        reap_rows.append(
            {
                "ts": now,
                "event": "subagent_reaped",
                "tool_call_id": tcid,
                "task_id": row.get("task_id"),
                "session_id": session_id,
                "duration_ms": duration_ms,
                "exit_status": "reaped",
                "reaped_reason": "session_end",
            }
        )
    return reap_rows


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        _emit_err(f"stdin parse failed: {exc}")
        return 0

    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id") or ""
    if not session_id:
        return 0

    reap_rows = _reap_orphans(session_id)
    _append_rows(reap_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
