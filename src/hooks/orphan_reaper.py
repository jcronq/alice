"""Periodic + boot-time orphan reaper for subagent dispatch events.

Design: ``cortex-memory/designs/2026-11-24-subagent-driven-running-now.md`` §2.3.

The two in-session hooks (``subagentstop-emit.py``, ``stop-session-reaper.py``)
catch clean exits and session-boundary closures. This module is the third net:
a standalone script that runs under a systemd user timer (periodic sweep) and
a oneshot unit on boot (host-reboot sweep). It emits ``subagent_reaped`` rows
with ``reaped_reason`` set to ``orphan`` or ``host_reboot`` accordingly.

Guarantees:
- **Idempotent** — a tool_call_id already present in the stop log is skipped
  on every subsequent run. Running the module twice in a row produces zero
  new rows.
- **Append-only** — rows are flock-serialised onto the same jsonl the hooks
  use; no in-place edits.
- **Stdlib-only** — fits the project's hook contract (no deps beyond py3.12).

CLI:
    python -m hooks.orphan_reaper                 # periodic sweep (default)
    python -m hooks.orphan_reaper --mode boot     # host-reboot sweep
    python -m hooks.orphan_reaper --threshold-seconds 7200
"""
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

START_LOG = Path("/state/worker/alice-task-dispatch.jsonl")
STOP_LOG = Path("/state/worker/alice-subagent-stop.jsonl")
SESSIONS_DIR = Path("/home/alice/.claude/sessions")
# Design §2.3: 2 h is the reap threshold. Faster trips live long-running turns;
# slower leaves dead agents on the dashboard.
DEFAULT_THRESHOLD_SECONDS = 2 * 60 * 60
# Design §2.3: a session file touched in the last 10 min is "live"; skip reaping
# its orphans on the periodic path (the host-reboot path ignores this).
SESSION_LIVE_WINDOW_SECONDS = 10 * 60

# Design §7.7: steerable rows get an extra check against the local Remote
# Control session registry (``claude agents --json`` surfaces every live
# session on this host, including those launched with ``--remote-control``).
# A steerable row whose ``session_name`` matches a live session is NOT reaped
# — the session is in flight and the agent may legitimately still be running
# past the 2h orphan threshold.
_CLAUDE_AGENTS_BIN = "claude"
_CLAUDE_AGENTS_TIMEOUT_S = 5.0


# --------------------------------------------------------------------------- #
# IO helpers
# --------------------------------------------------------------------------- #


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
    except OSError:
        return []


def _append_rows(rows: Iterable[dict], stop_log: Path) -> int:
    """Append reap rows to the stop log under flock. Return count written."""
    rows = list(rows)
    if not rows:
        return 0
    stop_log.parent.mkdir(parents=True, exist_ok=True)
    with stop_log.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return len(rows)


# --------------------------------------------------------------------------- #
# Core reaper
# --------------------------------------------------------------------------- #


#: Callable that returns the set of live Remote Control session *names* on
#: this host. The default implementation runs ``claude agents --json`` and
#: parses the response; tests inject a fake so the probe is deterministic.
LiveSessionNameProbe = Callable[[], frozenset[str]]


def _default_live_session_names() -> frozenset[str]:
    """Probe Claude Code for live Remote-Control session names.

    Returns the set of ``name`` fields from ``claude agents --json`` (the
    documented machine-readable listing of background agents / Remote
    Control sessions on this host). Any error → empty set, which makes
    the §7.7 check fall through to the session-file mtime path. This is
    the *safe* direction: a transient CLI failure must not cause us to
    skip-reap sessions that are actually dead.
    """
    try:
        proc = subprocess.run(
            [_CLAUDE_AGENTS_BIN, "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_AGENTS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    raw = (proc.stdout or "").strip()
    if not raw:
        return frozenset()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return frozenset()
    names: set[str] = set()
    items: Iterable
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("sessions") or payload.get("agents") or []
    else:
        items = ()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("session_name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return frozenset(names)


@dataclass(frozen=True)
class ReaperConfig:
    """Tunables for a single reaper pass.

    ``mode`` is purely a label that drives ``reaped_reason`` — the actual
    orphan-selection rules follow from the other fields.
    """

    mode: str = "orphan"  # "orphan" | "host_reboot"
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS
    sessions_dir: Path = SESSIONS_DIR
    start_log: Path = START_LOG
    stop_log: Path = STOP_LOG
    # ``now`` is injected for tests; real runs set None → wall clock.
    now: float | None = None
    # When True: treat every start as reap-worthy regardless of timestamp /
    # session-live check if its session file is missing. This is what the
    # host_reboot mode wants.
    reboot_mode: bool = False
    # Design §7.7: injected probe for live Remote Control session names.
    # Tests pass a lambda returning a fixed set; production uses the
    # module default (``claude agents --json``).
    live_session_names: LiveSessionNameProbe = field(
        default=_default_live_session_names
    )


def _session_is_live(cfg: ReaperConfig, session_id: str, now: float) -> bool:
    """Is ``~/.claude/sessions/<session_id>.jsonl`` fresh?

    Fresh = modified within ``SESSION_LIVE_WINDOW_SECONDS``. A nonexistent
    session file is **not live** — the session's host has gone away, and the
    subagent with it. See design §2.3 boot-sweep paragraph.
    """
    if not session_id:
        return False
    # Claude Code's sessions live under <sessions_dir>/<session_id>.jsonl OR
    # under a project subdir in sessions_dir. We check both shapes; the first
    # one that matches wins.
    candidates = [
        cfg.sessions_dir / f"{session_id}.jsonl",
        *cfg.sessions_dir.glob(f"**/{session_id}.jsonl"),
    ]
    for candidate in candidates:
        try:
            st = candidate.stat()
        except OSError:
            continue
        if (now - st.st_mtime) <= SESSION_LIVE_WINDOW_SECONDS:
            return True
        # File exists but is stale → treat as not-live.
        return False
    return False


def _session_file_exists(cfg: ReaperConfig, session_id: str) -> bool:
    """Lightweight existence check — any copy of the session file on disk."""
    if not session_id:
        return False
    candidates = [
        cfg.sessions_dir / f"{session_id}.jsonl",
        *cfg.sessions_dir.glob(f"**/{session_id}.jsonl"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return True
    return False


def reap(cfg: ReaperConfig | None = None) -> list[dict]:
    """Compute reap rows for the current state of the start + stop logs.

    Returns the list of new rows (also persists them unless caller uses
    :func:`compute_reap_rows` directly). Idempotent.
    """
    cfg = cfg or ReaperConfig()
    rows = compute_reap_rows(cfg)
    _append_rows(rows, cfg.stop_log)
    return rows


def compute_reap_rows(cfg: ReaperConfig) -> list[dict]:
    """Pure function: given config + on-disk state, what rows should be emitted?

    Split out from :func:`reap` so tests can compare the row list without
    serializing to disk.
    """
    now = cfg.now if cfg.now is not None else time.time()
    starts = _read_jsonl(cfg.start_log)
    stops = _read_jsonl(cfg.stop_log)

    closed_ids: set[str] = set()
    for row in stops:
        tcid = row.get("tool_call_id")
        if tcid:
            closed_ids.add(tcid)

    # Design §7.7: cache the live Remote-Control session set once per pass
    # so we don't fork+exec ``claude agents --json`` on every steerable
    # row. The probe runs iff at least one start row looks steerable —
    # avoids the subprocess cost on reap passes that have no steerable
    # orphans.
    live_names: frozenset[str] | None = None

    def _live_names() -> frozenset[str]:
        nonlocal live_names
        if live_names is None:
            try:
                live_names = cfg.live_session_names()
            except Exception:  # noqa: BLE001 — never block reaping on probe
                live_names = frozenset()
        return live_names

    reaped: list[dict] = []
    seen: set[str] = set()
    for row in starts:
        tcid = row.get("tool_call_id") or ""
        if not tcid or tcid in closed_ids or tcid in seen:
            continue
        seen.add(tcid)

        start_ts: float
        try:
            start_ts = float(row.get("ts", 0.0))
        except (TypeError, ValueError):
            start_ts = 0.0
        session_id = row.get("session_id") or ""

        # §7.5 fields — missing / absent is False per the design.
        is_steerable = bool(row.get("steerable", False))
        session_name = str(row.get("session_name") or "").strip() or None

        if cfg.reboot_mode:
            # Boot mode: reap every orphan whose session file is gone. A session
            # file that still exists on disk is from a prior Claude Code run that
            # exited cleanly (its Stop hook should have emitted session_end
            # reaps); leaving those for the periodic sweep is correct.
            if _session_file_exists(cfg, session_id):
                continue
            reaped_reason = "host_reboot"
        else:
            # Periodic mode: age-gate + session-liveness check.
            if (now - start_ts) < cfg.threshold_seconds:
                continue
            # §7.7: steerable rows get an extra check against the
            # Remote-Control registry — a live name there means the
            # session is still around and the subagent may legitimately
            # be past the 2h threshold. If the session_name is live,
            # skip reaping.
            if is_steerable and session_name and session_name in _live_names():
                continue
            if _session_is_live(cfg, session_id, now):
                continue
            reaped_reason = "orphan"

        try:
            duration_ms = int((now - start_ts) * 1000) if start_ts else None
        except (TypeError, ValueError):
            duration_ms = None

        reaped.append(
            {
                "ts": now,
                "event": "subagent_reaped",
                "tool_call_id": tcid,
                "task_id": row.get("task_id"),
                "session_id": session_id,
                "duration_ms": duration_ms,
                "exit_status": "reaped",
                "reaped_reason": reaped_reason,
            }
        )
    return reaped


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="orphan_reaper",
        description="Reap orphaned subagent starts missing a stop row.",
    )
    p.add_argument(
        "--mode",
        choices=("periodic", "boot"),
        default="periodic",
        help="periodic = age-gated sweep (default); boot = reap every orphan "
        "whose session file is gone (host-reboot recovery).",
    )
    p.add_argument(
        "--threshold-seconds",
        type=int,
        default=DEFAULT_THRESHOLD_SECONDS,
        help="Age (seconds) above which an open start is reap-eligible on the "
        "periodic sweep. Default: 7200 (2 h).",
    )
    p.add_argument(
        "--start-log",
        type=Path,
        default=START_LOG,
        help="Path to the dispatch-start jsonl. Default: %(default)s",
    )
    p.add_argument(
        "--stop-log",
        type=Path,
        default=STOP_LOG,
        help="Path to the stop jsonl. Default: %(default)s",
    )
    p.add_argument(
        "--sessions-dir",
        type=Path,
        default=SESSIONS_DIR,
        help="Claude Code sessions directory. Default: %(default)s",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = ReaperConfig(
        mode="host_reboot" if args.mode == "boot" else "orphan",
        threshold_seconds=args.threshold_seconds,
        sessions_dir=args.sessions_dir,
        start_log=args.start_log,
        stop_log=args.stop_log,
        reboot_mode=(args.mode == "boot"),
    )
    rows = reap(cfg)
    # Print a terse summary to stdout so the systemd journal captures it.
    print(f"orphan_reaper mode={args.mode} reaped={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
