"""Unit tests for ``src/hooks/orphan_reaper.py`` (design §2.3).

Covers idempotency, periodic-mode age gating, session-liveness skip, and the
host-reboot sweep acceptance test.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from hooks.orphan_reaper import ReaperConfig, compute_reap_rows, reap


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _cfg(tmp_path, *, mode: str = "orphan", **overrides) -> ReaperConfig:
    base = {
        "mode": mode,
        "threshold_seconds": 7200,
        "sessions_dir": tmp_path / "sessions",
        "start_log": tmp_path / "start.jsonl",
        "stop_log": tmp_path / "stop.jsonl",
        "now": None,
        "reboot_mode": mode == "host_reboot",
    }
    base.update(overrides)
    return ReaperConfig(**base)


def test_idempotency_second_run_zero_new_rows(tmp_path):
    """Design §2.3: second run emits zero new rows."""
    cfg = _cfg(tmp_path, now=time.time())
    now = cfg.now
    # Start row aged well past the 2 h threshold, session file missing.
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-old",
                "tool_call_id": "toolu_old",
                "session_id": "ghost-session",
            }
        ],
    )
    first = reap(cfg)
    assert len(first) == 1
    assert first[0]["reaped_reason"] == "orphan"
    second = reap(cfg)
    assert second == []


def test_young_orphan_not_reaped_on_periodic(tmp_path):
    """An orphan younger than the threshold is spared on the periodic path."""
    now = time.time()
    cfg = _cfg(tmp_path, now=now)
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 60,  # 1 minute old
                "task_id": "task-fresh",
                "tool_call_id": "toolu_fresh",
                "session_id": "fresh-session",
            }
        ],
    )
    assert compute_reap_rows(cfg) == []


def test_live_session_skips_reap(tmp_path):
    """A session file touched in the last 10 min keeps the orphan alive."""
    now = time.time()
    cfg = _cfg(tmp_path, now=now)
    sess_file = cfg.sessions_dir / "live-sess.jsonl"
    sess_file.parent.mkdir(parents=True, exist_ok=True)
    sess_file.write_text("{}\n", encoding="utf-8")
    # Force mtime to "now" so the liveness check passes.
    import os
    os.utime(sess_file, (now, now))

    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-live",
                "tool_call_id": "toolu_live",
                "session_id": "live-sess",
            }
        ],
    )
    assert compute_reap_rows(cfg) == []


def test_stale_session_falls_through(tmp_path):
    """Session file present but older than the live window → orphan is reaped."""
    now = time.time()
    cfg = _cfg(tmp_path, now=now)
    sess_file = cfg.sessions_dir / "stale-sess.jsonl"
    sess_file.parent.mkdir(parents=True, exist_ok=True)
    sess_file.write_text("{}\n", encoding="utf-8")
    import os
    stale_mtime = now - 20 * 60  # older than SESSION_LIVE_WINDOW_SECONDS (10 min)
    os.utime(sess_file, (stale_mtime, stale_mtime))

    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-stale",
                "tool_call_id": "toolu_stale",
                "session_id": "stale-sess",
            }
        ],
    )
    rows = compute_reap_rows(cfg)
    assert len(rows) == 1
    assert rows[0]["reaped_reason"] == "orphan"


def test_host_reboot_mode_reaps_missing_session_file(tmp_path):
    """Design §2.3 host-reboot acceptance test.

    Fake a missing session file → assert one reap row with
    ``reaped_reason=host_reboot``.
    """
    now = time.time()
    cfg = _cfg(tmp_path, mode="host_reboot", now=now, reboot_mode=True)
    # Deliberately do not create cfg.sessions_dir — simulates "host rebooted,
    # all session files are gone".
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 60,  # even young — reboot mode ignores age
                "task_id": "task-boot",
                "tool_call_id": "toolu_boot",
                "session_id": "pre-reboot-session",
            }
        ],
    )
    rows = compute_reap_rows(cfg)
    assert len(rows) == 1
    assert rows[0]["reaped_reason"] == "host_reboot"
    assert rows[0]["exit_status"] == "reaped"


def test_host_reboot_skips_rows_whose_session_still_exists(tmp_path):
    """A session file that survived the "reboot" → leave for the periodic sweep."""
    now = time.time()
    cfg = _cfg(tmp_path, mode="host_reboot", now=now, reboot_mode=True)
    sess_file = cfg.sessions_dir / "survivor.jsonl"
    sess_file.parent.mkdir(parents=True, exist_ok=True)
    sess_file.write_text("{}\n", encoding="utf-8")

    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 60,
                "task_id": "task-live",
                "tool_call_id": "toolu_live",
                "session_id": "survivor",
            }
        ],
    )
    assert compute_reap_rows(cfg) == []


def test_closed_tool_call_ids_skipped(tmp_path):
    """Rows with any existing stop (clean or reap) are not re-reaped."""
    now = time.time()
    cfg = _cfg(tmp_path, now=now)
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-closed",
                "tool_call_id": "toolu_closed",
                "session_id": "whatever",
            },
            {
                "ts": now - 10_000,
                "task_id": "task-open",
                "tool_call_id": "toolu_open",
                "session_id": "whatever2",
            },
        ],
    )
    _write_jsonl(
        cfg.stop_log,
        [
            {
                "ts": now - 9_000,
                "event": "subagent_stop",
                "tool_call_id": "toolu_closed",
                "task_id": "task-closed",
                "session_id": "whatever",
                "duration_ms": 1000,
                "exit_status": "ok",
                "reaped_reason": None,
            }
        ],
    )
    rows = compute_reap_rows(cfg)
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "toolu_open"


def test_missing_tool_call_id_skipped(tmp_path):
    """A start row with empty tool_call_id cannot be keyed — skip it."""
    now = time.time()
    cfg = _cfg(tmp_path, now=now)
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-no-key",
                "tool_call_id": "",
                "session_id": "ghost",
            }
        ],
    )
    assert compute_reap_rows(cfg) == []


# ─── Steerable-aware check (design §7.7) ─────────────────────────────


def test_steerable_row_with_live_session_name_not_reaped(tmp_path):
    """§7.7: a steerable row whose session_name is in the live-RC registry
    is spared even past the orphan threshold."""
    now = time.time()
    cfg = _cfg(
        tmp_path,
        now=now,
        live_session_names=lambda: frozenset({"task-1025"}),
    )
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,  # well past threshold
                "task_id": "task-1025",
                "tool_call_id": "toolu_live_steer",
                "session_id": "steerable-sess",
                "steerable": True,
                "session_name": "task-1025",
            }
        ],
    )
    assert compute_reap_rows(cfg) == []


def test_steerable_row_without_live_session_name_is_reaped(tmp_path):
    """Same steerable row, session_name absent from live registry → reaped."""
    now = time.time()
    cfg = _cfg(
        tmp_path,
        now=now,
        live_session_names=lambda: frozenset(),
    )
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-1025",
                "tool_call_id": "toolu_dead_steer",
                "session_id": "steerable-dead",
                "steerable": True,
                "session_name": "task-1025",
            }
        ],
    )
    rows = compute_reap_rows(cfg)
    assert len(rows) == 1
    assert rows[0]["reaped_reason"] == "orphan"


def test_live_session_probe_failure_falls_back_safely(tmp_path):
    """Probe raises → treated as empty set, row is reaped (safe direction)."""
    now = time.time()

    def _broken_probe() -> frozenset[str]:
        raise RuntimeError("claude agents --json: ENOENT")

    cfg = _cfg(tmp_path, now=now, live_session_names=_broken_probe)
    _write_jsonl(
        cfg.start_log,
        [
            {
                "ts": now - 10_000,
                "task_id": "task-9",
                "tool_call_id": "toolu_broken_probe",
                "session_id": "whatever",
                "steerable": True,
                "session_name": "task-9",
            }
        ],
    )
    rows = compute_reap_rows(cfg)
    assert len(rows) == 1
