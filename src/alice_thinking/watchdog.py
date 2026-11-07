"""Thinker watchdog — detect and recover from hung thinking wakes.

Thinking has been observed hanging in ``epoll_wait`` on a leaked socket
fd from the kernel SDK's stream transport: the wake's logical work has
already finished (the wake.md file is on disk), but the Python process
never exits. While that process clings to ``/state/worker/thinking.lock``
the s6 supervisor's next ``flock -n`` skips, and the cadence stalls
silently — only manual intervention recovers the loop.

This module is the deterministic floor for that recovery. A separate
s6 longrun (``alice-thinker-watchdog``) calls :func:`main` every 30s.
The watchdog is *additive*: it doesn't manage the thinker, it just
kills hung processes and lets s6 spin a fresh wake on the next cadence.

Decision rules — one tick:

* No process holding the lock and no orphan lock fd  → ``"idle"``.
* No process holding the lock but the lock file exists (held by a
  vanished pid)                                       → ``"orphan_lock"``.
* Process alive AND the most recent wake-file mtime
  is fresher than the staleness threshold M           → ``"working"``.
* Process alive AND wake-file is older than M         → ``"stuck"``.

Staleness threshold M (seconds):

    M = 2 * current_cadence + 60

``current_cadence`` is read from ``/state/worker/next-thinking-interval-seconds``
(the file ``wake.py`` writes via ``backoff.write_interval_atomic``);
fallback is 600 if the file is missing or malformed. So in active mode
(5-min cadence) M = 25 min; in sleep with the 40-min backoff M = 200 min.

On ``"stuck"`` we send SIGTERM, wait up to 30s, then SIGKILL if the
process is still alive. A ``thinker_watchdog_intervention`` record
is appended to ``memory/events.jsonl`` with the timeline (wake-file
delta, ``/proc/<pid>/wchan``, whether SIGTERM was enough). On
``"orphan_lock"`` we unlink the lock file so the next supervisor
iteration can recreate it.

Phase 2 (not in this module yet): an LLM judgment call layered on top
of the deterministic rules for the gray cases — long Stage D synthesis
vs. an actual hang. The hooks (the ``Decision`` enum and ``intervene``
contract) leave room for a "soft stuck" path that asks a local model
"is this still working?" before escalating to SIGTERM. See
``docs/designs/thinker-watchdog.md`` for the placeholder.
"""

from __future__ import annotations

import enum
import errno
import json
import os
import pathlib
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


DEFAULT_LOCK_PATH = pathlib.Path("/state/worker/thinking.lock")
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_EVENTS_LOG = DEFAULT_MIND / "memory" / "events.jsonl"

INTERVAL_FILE_NAME = "next-thinking-interval-seconds"

# Cadence used when the supervisor's interval file is missing or unreadable.
# Matches the spec's documented fallback: 600s (10 min) keeps the watchdog
# permissive enough to never false-positive a normal wake.
DEFAULT_CADENCE_SECONDS = 600

# Multiplier on the current cadence + a constant cushion. Active wakes
# (5-min cadence) get a 25-minute window; sleep@40min gets 200 minutes.
#
# The 5x multiplier covers native workflows (Stage B in
# ``alice_thinking.workflows.stage_b``) that legitimately take 15-20
# minutes when their LLM subroutines fan out. The earlier 2x
# (660s active) would SIGTERM healthy workflows. Phase 2 (per-step
# heartbeat) is the structural fix that lets us tighten this back down;
# until heartbeats land, 5x is the safe floor — it still beats the
# 21-minute manual-intervention window we hit before the watchdog
# existed.
STALE_CADENCE_MULTIPLIER = 5
STALE_CUSHION_SECONDS = 60

# How long to wait for SIGTERM to be honored before escalating to SIGKILL.
# 30s mirrors the spec; long enough for Python's atexit/finally chains
# but short enough that a hung process doesn't keep blocking the cadence.
SIGTERM_GRACE_SECONDS = 30


class Decision(str, enum.Enum):
    """Outcome of a single watchdog tick. String-valued for log clarity."""

    IDLE = "idle"
    WORKING = "working"
    STUCK = "stuck"
    ORPHAN_LOCK = "orphan_lock"


@dataclass
class WatchdogState:
    """Per-tick snapshot — what the watchdog observed and decided.

    Captured up-front so the intervention path and the event payload
    see the same numbers; avoids re-reading mtimes between the
    decision and the kill.
    """

    decision: Decision
    pid: Optional[int] = None
    cadence_seconds: int = DEFAULT_CADENCE_SECONDS
    stale_threshold_seconds: int = 0
    wake_file: Optional[pathlib.Path] = None
    wake_file_age_seconds: Optional[float] = None
    wchan: Optional[str] = None
    extra: dict = field(default_factory=dict)


# ---------- pid + lock discovery ----------


def _read_proc_locks() -> list[tuple[int, int]]:
    """Return ``[(pid, inode), ...]`` for every advisory write flock.

    ``/proc/locks`` lines look like::

        1: FLOCK  ADVISORY  WRITE 1100086 fd:00:5246348 0 EOF

    We only care about FLOCK advisory write entries — that's what the
    s6 supervisor's ``flock -n`` takes. POSIX locks live on the same
    file but never collide with the bash wrapper.
    """
    out: list[tuple[int, int]] = []
    try:
        text = pathlib.Path("/proc/locks").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        # parts[0] is the lock id (e.g. "1:"), parts[1]=type, parts[2]=lifetime,
        # parts[3]=access, parts[4]=pid, parts[5]="major:minor:inode".
        if parts[1] != "FLOCK" or parts[3] != "WRITE":
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        triple = parts[5].split(":")
        if len(triple) != 3:
            continue
        try:
            inode = int(triple[2])
        except ValueError:
            continue
        out.append((pid, inode))
    return out


def find_thinker_pid(
    lock_path: pathlib.Path = DEFAULT_LOCK_PATH,
    *,
    locks_text: Optional[str] = None,
) -> Optional[int]:
    """Return the PID currently holding the flock on ``lock_path``.

    Resolution path: stat the lock file's inode, then walk
    ``/proc/locks`` for any advisory FLOCK on that inode. Returns the
    holder's PID, or ``None`` if no live process holds the lock.

    The flock holder is the bash supervisor's ``flock`` invocation,
    which exec's ``s6-setuidgid alice /usr/local/bin/alice-think`` —
    so the PID we discover *is* the python process (or its parent
    bash, depending on whether ``flock -c`` or the wrapper form is
    used; today's supervisor uses the wrapper form, so the holder is
    the python process directly).

    ``locks_text`` is a test seam — pass a synthetic ``/proc/locks``
    snapshot to avoid touching the real filesystem.
    """
    try:
        st = lock_path.stat()
    except OSError:
        return None
    target_inode = st.st_ino
    locks = (
        _parse_locks_text(locks_text) if locks_text is not None else _read_proc_locks()
    )
    for pid, inode in locks:
        if inode == target_inode:
            return pid
    return None


def _parse_locks_text(text: str) -> list[tuple[int, int]]:
    """Test-seam parser matching :func:`_read_proc_locks` line format."""
    out: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        if parts[1] != "FLOCK" or parts[3] != "WRITE":
            continue
        try:
            pid = int(parts[4])
            triple = parts[5].split(":")
            inode = int(triple[2])
        except (ValueError, IndexError):
            continue
        out.append((pid, inode))
    return out


def _proc_alive(pid: int) -> bool:
    """True iff ``/proc/<pid>`` exists. Cheaper + safer than ``kill 0``
    when the watchdog runs as a different uid than the thinker."""
    return pathlib.Path(f"/proc/{pid}").exists()


def _read_proc_status(pid: int) -> dict[str, str]:
    """Parse ``/proc/<pid>/status`` into a flat dict. Returns ``{}`` on any
    read failure — missing /proc entries just drop out of the subsequent
    descendant walk rather than raising.
    """
    try:
        text = pathlib.Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


def _list_proc_pids() -> list[int]:
    """Enumerate all numeric ``/proc/<pid>/`` entries."""
    out: list[int] = []
    try:
        for entry in pathlib.Path("/proc").iterdir():
            name = entry.name
            if name.isdigit():
                try:
                    out.append(int(name))
                except ValueError:
                    continue
    except OSError:
        return out
    return out


def _parse_status_ppid(info: dict[str, str]) -> Optional[int]:
    try:
        return int(info.get("PPid", ""))
    except (TypeError, ValueError):
        return None


def _parse_status_euid(info: dict[str, str]) -> Optional[int]:
    """``Uid:`` line is ``real\\teffective\\tsaved\\tfsuid``. We care about
    the effective uid — that's what ``kill(2)`` checks."""
    parts = info.get("Uid", "").split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _resolve_alice_child_pid(
    wrapper_pid: int,
    *,
    our_euid: Optional[int] = None,
    proc_status=None,
    list_pids=None,
) -> Optional[int]:
    """Walk ``/proc`` from ``wrapper_pid`` and return the first descendant
    whose effective uid matches ours.

    The s6 supervisor launches each wake as::

        flock -n /state/worker/thinking.lock s6-setuidgid alice /usr/local/bin/alice-think

    ``/proc/locks`` reports the ``flock`` parent (root-owned), but the
    python process we actually need to SIGTERM lives further down the
    subtree after ``s6-setuidgid`` drops to uid=alice. Signalling the
    root parent from the alice-uid watchdog raises EPERM on every tick.

    Resolution strategy:

    * If the wrapper itself is already alice-owned, return it unchanged.
    * Otherwise BFS the /proc tree from ``wrapper_pid``. Return the
      first descendant whose effective uid equals ``our_euid``.
    * If the wrapper's own /proc entry is missing (orphaned, race) or
      no alice-owned descendant exists (wrapper dying alone), return
      ``None`` so the caller can log a warning instead of signalling
      root.

    ``proc_status`` and ``list_pids`` are test seams.
    """
    if our_euid is None:
        our_euid = os.geteuid()
    if proc_status is None:
        proc_status = _read_proc_status
    if list_pids is None:
        list_pids = _list_proc_pids

    wrapper_info = proc_status(wrapper_pid)
    if not wrapper_info:
        # /proc/<wrapper_pid> missing — wrapper vanished between the lock
        # read and now. Don't try to kill a PID we can't even inspect.
        return None
    wrapper_euid = _parse_status_euid(wrapper_info)
    if wrapper_euid == our_euid:
        return wrapper_pid

    # Build (pid → ppid) and (pid → euid) maps in one scan so BFS doesn't
    # re-read /proc for every candidate.
    children_by_parent: dict[int, list[int]] = {}
    euids: dict[int, int] = {}
    for p in list_pids():
        if p == wrapper_pid:
            continue
        info = proc_status(p)
        if not info:
            continue
        ppid = _parse_status_ppid(info)
        euid = _parse_status_euid(info)
        if ppid is not None:
            children_by_parent.setdefault(ppid, []).append(p)
        if euid is not None:
            euids[p] = euid

    frontier = [wrapper_pid]
    visited = {wrapper_pid}
    while frontier:
        next_frontier: list[int] = []
        for current in frontier:
            for child in children_by_parent.get(current, []):
                if child in visited:
                    continue
                visited.add(child)
                if euids.get(child) == our_euid:
                    return child
                next_frontier.append(child)
        frontier = next_frontier
    return None


def _read_wchan(pid: int) -> Optional[str]:
    """Read ``/proc/<pid>/wchan`` — the kernel function the task is
    sleeping in. ``ep_poll`` is the leak signature for the SDK socket
    bug. Returns ``None`` if the file is unreadable (process gone,
    hidepid, etc.)."""
    try:
        return pathlib.Path(f"/proc/{pid}/wchan").read_text().strip() or None
    except OSError:
        return None


# ---------- wake-file freshness ----------


def wake_file_age(
    mind: pathlib.Path,
    *,
    today: Optional[str] = None,
    now: Optional[float] = None,
) -> tuple[Optional[pathlib.Path], Optional[float]]:
    """Return ``(path, age_seconds)`` for the most recent wake file.

    Looks under ``mind/inner/thoughts/<today>/*.md``. ``today`` defaults
    to UTC today (``YYYY-MM-DD``) — ``wake.py`` writes its summary to
    ``<today-UTC>/`` per the prelude convention (``date -u +%Y-%m-%d``),
    so the watchdog must query the same key. Using local time instead
    would silently diverge after UTC midnight (local Nov 6 ~19:00 EST):
    the watchdog looks at yesterday-UTC's dir, sees ~4h-old stale files,
    and declares STUCK within minutes of a fresh start. Issue #785 bug 2.

    Returns ``(None, None)`` if the directory is missing, empty, or only
    contains today's-not-written-yet files.

    The watchdog uses this as the heartbeat: ``wake.py`` writes the
    summary file at logical-work completion. If the process is alive
    but no file has been touched within M seconds, the wake is hung
    in cleanup (the SDK socket leak), not still working.
    """
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if now is None:
        now = time.time()

    day_dir = mind / "inner" / "thoughts" / today
    if not day_dir.is_dir():
        return None, None

    candidates = list(day_dir.glob("*.md"))
    if not candidates:
        return None, None

    newest: Optional[pathlib.Path] = None
    newest_mtime = -1.0
    for p in candidates:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest = p
            newest_mtime = mtime
    if newest is None:
        return None, None
    return newest, max(0.0, now - newest_mtime)


# ---------- cadence read ----------


def current_cadence_seconds(
    state_dir: pathlib.Path = DEFAULT_STATE_DIR,
    *,
    fallback: int = DEFAULT_CADENCE_SECONDS,
) -> int:
    """Read the supervisor's interval file with a sane fallback.

    Independent of :func:`alice_thinking.backoff.read_interval` — the
    backoff module *clamps* into ``[BASE, MAX]`` because the supervisor
    must never sleep faster or slower than the design allows. The
    watchdog's read is *advisory*: a malformed file just falls back to
    600 so the staleness threshold stays permissive.
    """
    path = state_dir / INTERVAL_FILE_NAME
    try:
        text = path.read_text().strip()
    except OSError:
        return fallback
    try:
        v = int(text)
    except ValueError:
        return fallback
    if v <= 0:
        return fallback
    return v


def stale_threshold_seconds(cadence: int) -> int:
    """``5 * cadence + 60`` — the M from the spec (post-PR-#19 update).

    Active mode (5-min cadence) → 25 min.
    Sleep@40min                 → 200 min.

    Sized to cover native Stage B workflow runs (LLM-subroutine fan-out
    can take 15-20 minutes). Phase 2 (per-step heartbeats) lets us
    tighten this back down.
    """
    return STALE_CADENCE_MULTIPLIER * int(cadence) + STALE_CUSHION_SECONDS


# ---------- decision ----------


def is_stuck(
    pid: Optional[int],
    wake_age: Optional[float],
    cadence: int,
    *,
    lock_exists: bool = False,
) -> Decision:
    """Apply the deterministic rules and return one decision.

    The watchdog never sees the cluster of (pid, wake_age, cadence,
    lock_exists) inputs split across files — they're all derived in
    a single tick from the snapshot.
    """
    if pid is None:
        if lock_exists:
            return Decision.ORPHAN_LOCK
        return Decision.IDLE
    threshold = stale_threshold_seconds(cadence)
    # No wake file at all is *not* automatically stuck — a fresh thinker
    # process that has yet to write its first wake file looks indistinguishable
    # from a hung one. We treat that as "working" until the threshold has
    # been exceeded relative to the process's own start time.
    if wake_age is None:
        return _classify_no_wake_file(pid, threshold)
    if wake_age >= threshold:
        return Decision.STUCK
    return Decision.WORKING


def _proc_age_seconds(pid: int, *, now: Optional[float] = None) -> Optional[float]:
    """Wall-clock age of the process. Reads ``/proc/<pid>``'s mtime —
    that's the dirent for the process directory, which Linux sets at
    fork. Returns ``None`` if the process is gone."""
    try:
        st = pathlib.Path(f"/proc/{pid}").stat()
    except OSError:
        return None
    return (now if now is not None else time.time()) - st.st_mtime


def _classify_no_wake_file(pid: int, threshold: int) -> Decision:
    """Process alive but no wake file yet. If the process has been
    running longer than the threshold without producing one, treat as
    stuck — this catches the early-hang case where the SDK leaks
    before any logical work writes its summary."""
    age = _proc_age_seconds(pid)
    if age is None:
        return Decision.IDLE
    if age >= threshold:
        return Decision.STUCK
    return Decision.WORKING


# ---------- intervention ----------


def _kill(pid: int, sig: int) -> bool:
    """Best-effort ``os.kill``. Returns False if the process was already
    gone — that's a normal race, not an error."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise


def _sleep_until_dead(
    pid: int,
    *,
    timeout_seconds: int,
    poll_interval: float = 0.5,
    proc_alive=_proc_alive,
    sleep=time.sleep,
    now=time.time,
) -> bool:
    """Poll ``/proc/<pid>`` until it disappears or the timeout fires.
    Returns True if the process exited. Test seams (``proc_alive``,
    ``sleep``, ``now``) keep this synchronous + deterministic in tests."""
    deadline = now() + timeout_seconds
    while now() < deadline:
        if not proc_alive(pid):
            return True
        sleep(poll_interval)
    return not proc_alive(pid)


def intervene(
    pid: int,
    *,
    grace_seconds: int = SIGTERM_GRACE_SECONDS,
    kill=_kill,
    proc_alive=_proc_alive,
    sleep=time.sleep,
    now=time.time,
) -> dict:
    """SIGTERM, wait, SIGKILL if still alive. Return telemetry.

    Telemetry shape::

        {
            "sigterm_sent": bool,
            "sigterm_sufficient": bool,
            "sigkill_sent": bool,
            "elapsed_seconds": float,
            "exited": bool,
        }

    ``sigterm_sufficient`` is the headline number — if it's False, the
    SDK socket leak escalated past Python's signal handlers and the
    only recovery was a hard kill. Phase 2 will use that as a hint
    for the LLM judgment loop.
    """
    started = now()
    sigterm_sent = kill(pid, signal.SIGTERM)
    if not sigterm_sent:
        # Process was already gone before we could SIGTERM it.
        return {
            "sigterm_sent": False,
            "sigterm_sufficient": True,
            "sigkill_sent": False,
            "elapsed_seconds": 0.0,
            "exited": True,
        }

    exited = _sleep_until_dead(
        pid,
        timeout_seconds=grace_seconds,
        proc_alive=proc_alive,
        sleep=sleep,
        now=now,
    )
    if exited:
        return {
            "sigterm_sent": True,
            "sigterm_sufficient": True,
            "sigkill_sent": False,
            "elapsed_seconds": now() - started,
            "exited": True,
        }

    sigkill_sent = kill(pid, signal.SIGKILL)
    # Give SIGKILL a brief window to be reaped — the kernel won't
    # block on it but ``/proc/<pid>`` may take a tick to clear.
    _ = _sleep_until_dead(
        pid,
        timeout_seconds=2,
        proc_alive=proc_alive,
        sleep=sleep,
        now=now,
    )
    return {
        "sigterm_sent": True,
        "sigterm_sufficient": False,
        "sigkill_sent": sigkill_sent,
        "elapsed_seconds": now() - started,
        "exited": not proc_alive(pid),
    }


# ---------- event emission ----------


def _append_event(events_log: pathlib.Path, payload: dict) -> None:
    """Append one JSONL line. Best-effort — observability never raises.

    We mirror :class:`core.events.EventLogger`'s on-disk shape
    (``ts``, ``event``, plus payload fields) so the viewer can tail
    both ``thinking.log`` and ``memory/events.jsonl`` with the same
    parser.
    """
    record = {"ts": time.time(), "event": "thinker_watchdog_intervention", **payload}
    try:
        events_log.parent.mkdir(parents=True, exist_ok=True)
        with events_log.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


def _orphan_lock_event(events_log: pathlib.Path, lock_path: pathlib.Path) -> None:
    """Lighter-weight cousin of :func:`_append_event` for orphan-lock
    cleanup — separate event name so the viewer / dashboards don't
    confuse "we killed something" with "we cleared a stale file"."""
    record = {
        "ts": time.time(),
        "event": "thinker_watchdog_orphan_lock",
        "lock_path": str(lock_path),
    }
    try:
        events_log.parent.mkdir(parents=True, exist_ok=True)
        with events_log.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


# ---------- main ----------


def run_tick(
    *,
    lock_path: pathlib.Path = DEFAULT_LOCK_PATH,
    state_dir: pathlib.Path = DEFAULT_STATE_DIR,
    mind: pathlib.Path = DEFAULT_MIND,
    events_log: Optional[pathlib.Path] = None,
    today: Optional[str] = None,
    now: Optional[float] = None,
) -> WatchdogState:
    """One watchdog tick. Pure-ish: side effects are kill + event-append
    only. Returns the snapshot for the caller to inspect (used by tests
    and by ``main()`` for stderr summaries)."""
    if events_log is None:
        events_log = mind / "memory" / "events.jsonl"
    if now is None:
        now = time.time()

    raw_pid = find_thinker_pid(lock_path)
    # The /proc/locks holder is the root-owned flock wrapper; walk the
    # subtree to the alice-owned python descendant we can actually
    # SIGTERM from this (uid=alice) watchdog. See issue #785 bug 1.
    if raw_pid is not None:
        pid = _resolve_alice_child_pid(raw_pid)
        if pid is None:
            # Wrapper exists but no alice-owned descendant — either the
            # python child already exited (wrapper hasn't reaped yet)
            # or the subtree is somehow orphaned. Don't try to signal
            # root; treat as "no actionable process" and let the next
            # tick re-evaluate.
            print(
                f"alice-thinker-watchdog: wrapper pid {raw_pid} has no "
                f"alice-owned descendant; skipping tick",
                file=sys.stderr,
            )
    else:
        pid = None

    lock_exists = lock_path.exists()
    cadence = current_cadence_seconds(state_dir)
    threshold = stale_threshold_seconds(cadence)
    wake_path, wake_age = wake_file_age(mind, today=today, now=now)
    decision = is_stuck(pid, wake_age, cadence, lock_exists=lock_exists)

    state = WatchdogState(
        decision=decision,
        pid=pid,
        cadence_seconds=cadence,
        stale_threshold_seconds=threshold,
        wake_file=wake_path,
        wake_file_age_seconds=wake_age,
        wchan=_read_wchan(pid) if pid is not None else None,
    )
    if raw_pid is not None and raw_pid != pid:
        state.extra["wrapper_pid"] = raw_pid

    if decision == Decision.STUCK and pid is not None:
        timeline = intervene(pid)
        _append_event(
            events_log,
            {
                "pid": pid,
                "wchan": state.wchan,
                "wake_file": str(wake_path) if wake_path else None,
                "wake_file_age_seconds": wake_age,
                "cadence_seconds": cadence,
                "stale_threshold_seconds": threshold,
                **timeline,
            },
        )
        state.extra["intervention"] = timeline
    elif decision == Decision.ORPHAN_LOCK:
        try:
            lock_path.unlink()
            state.extra["orphan_cleared"] = True
        except OSError as exc:
            state.extra["orphan_cleared"] = False
            state.extra["orphan_error"] = str(exc)
        _orphan_lock_event(events_log, lock_path)

    return state


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry. One tick per invocation; the s6 service loops at 30s.

    Exit code is 0 on every clean tick (idle / working / stuck-and-
    handled / orphan-lock-and-cleared). Non-zero only if an unexpected
    exception escaped — the longrun will then be restarted by s6, which
    is the right blast radius for "the watchdog itself is broken".
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="One watchdog tick over the thinking flock."
    )
    parser.add_argument("--lock", default=str(DEFAULT_LOCK_PATH))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--mind", default=str(DEFAULT_MIND))
    parser.add_argument(
        "--events-log",
        default=None,
        help="override events.jsonl path (default: <mind>/memory/events.jsonl)",
    )
    parser.add_argument(
        "--echo", action="store_true", help="echo the decision to stderr"
    )
    args = parser.parse_args(argv)

    events_log = (
        pathlib.Path(args.events_log)
        if args.events_log
        else pathlib.Path(args.mind) / "memory" / "events.jsonl"
    )

    try:
        state = run_tick(
            lock_path=pathlib.Path(args.lock),
            state_dir=pathlib.Path(args.state_dir),
            mind=pathlib.Path(args.mind),
            events_log=events_log,
        )
    except Exception as exc:  # noqa: BLE001
        # The watchdog can't quietly swallow its own bugs — let s6
        # restart the longrun so we don't drift in a broken state.
        print(f"alice-thinker-watchdog: tick failed: {exc}", file=sys.stderr)
        return 1

    if args.echo:
        sys.stderr.write(
            f"watchdog: decision={state.decision.value} pid={state.pid} "
            f"cadence={state.cadence_seconds}s stale_at={state.stale_threshold_seconds}s "
            f"wake_age={state.wake_file_age_seconds} wchan={state.wchan}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
