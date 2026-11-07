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
from datetime import datetime
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


def _read_wchan(pid: int) -> Optional[str]:
    """Read ``/proc/<pid>/wchan`` — the kernel function the task is
    sleeping in. ``ep_poll`` is the leak signature for the SDK socket
    bug. Returns ``None`` if the file is unreadable (process gone,
    hidepid, etc.)."""
    try:
        return pathlib.Path(f"/proc/{pid}/wchan").read_text().strip() or None
    except OSError:
        return None


# ---------- signal-target resolution ----------
#
# The flock holder (per :func:`find_thinker_pid`) is the root-owned
# supervisor/wrapper PID — ``flock`` opens the lock as root before the
# ``s6-setuidgid`` / ``timeout`` fork flips to uid=alice and forks the
# real ``python -m alice_thinking`` process. ``os.kill`` from uid=alice
# against that root pid is ``PermissionError``. These helpers resolve
# the signal target to the alice-owned python descendant: BFS the pid
# tree from the root, pick the euid-matched descendant whose cmdline
# positively identifies the python worker (so we skip past an already-
# alice-owned ``timeout`` monitor whose signal-forwarding is both
# indirect and can't forward ``SIGKILL``).


def _proc_ppid(pid: int, *, stat_text: Optional[str] = None) -> Optional[int]:
    """Parse field 4 (ppid) of ``/proc/<pid>/stat``.

    The comm field (field 2) is parenthesized and may itself contain
    spaces and nested parens (e.g. ``(some (nested) thing)``), so we
    split on the *last* ``)`` before tokenizing the remainder — the
    classic ``/proc/<pid>/stat`` parsing gotcha.

    ``stat_text`` is a test seam. Returns ``None`` if the pid is gone
    or the stat line is unparseable.
    """
    if stat_text is None:
        try:
            stat_text = pathlib.Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
    end = stat_text.rfind(")")
    if end == -1:
        return None
    rest = stat_text[end + 1 :].split()
    # After the comm close-paren, the next fields are: state, ppid, pgrp, ...
    if len(rest) < 2:
        return None
    try:
        return int(rest[1])
    except ValueError:
        return None


def _proc_euid(pid: int) -> Optional[int]:
    """Return ``st_uid`` of ``/proc/<pid>`` — the process's euid as
    exposed through the ``/proc`` dirent. Returns ``None`` if the pid
    is gone."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None


def _proc_cmdline(pid: int) -> str:
    """Return ``/proc/<pid>/cmdline`` with NUL separators replaced by
    spaces, stripped. Returns ``""`` if unreadable."""
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _scan_proc_tree() -> dict[int, int]:
    """One pass over ``/proc/[0-9]+/stat`` → ``{pid: ppid}``.

    Built once per resolution call; the container's process count is
    small (dozens), so this is cheap at a 30s cadence. Pids we can't
    parse are silently skipped.
    """
    tree: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return tree
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            pid = int(entry)
        except ValueError:
            continue
        ppid = _proc_ppid(pid)
        if ppid is None:
            continue
        tree[pid] = ppid
    return tree


def find_signal_target(
    root_pid: int,
    *,
    want_euid: Optional[int] = None,
    name_hint: str = "alice_thinking",
    tree: Optional[dict[int, int]] = None,
) -> int:
    """Resolve the actual PID to signal.

    BFS the descendants of ``root_pid`` (using ``tree``, or a fresh
    :func:`_scan_proc_tree` if not given — ``tree`` is the test seam,
    mirroring :func:`find_thinker_pid`'s ``locks_text`` arg). Consider
    ``root_pid`` itself plus all descendants. Among candidates whose
    euid equals ``want_euid`` (default ``os.geteuid()``):

    * Prefer one whose cmdline contains ``name_hint`` (positively
      identifies the python worker over an intermediate wrapper
      that's already alice-owned, e.g. ``timeout``).
    * Else the deepest euid match found.
    * Else return ``root_pid`` unchanged — status-quo fallback, never
      worse than the pre-fix behavior. If ``/proc`` visibility ever
      tightens such that no descendant is visible, we degrade to the
      prior bug rather than silently doing nothing.
    """
    if want_euid is None:
        want_euid = os.geteuid()
    if tree is None:
        tree = _scan_proc_tree()

    # BFS: build (pid, depth) list of root + its descendants.
    children: dict[int, list[int]] = {}
    for child, parent in tree.items():
        children.setdefault(parent, []).append(child)

    order: list[tuple[int, int]] = [(root_pid, 0)]
    seen: set[int] = {root_pid}
    i = 0
    while i < len(order):
        pid, depth = order[i]
        i += 1
        for child in children.get(pid, ()):
            if child in seen:
                continue
            seen.add(child)
            order.append((child, depth + 1))

    best_named: Optional[tuple[int, int]] = None  # (depth, pid)
    best_euid: Optional[tuple[int, int]] = None
    for pid, depth in order:
        euid = _proc_euid(pid)
        if euid != want_euid:
            continue
        if name_hint and name_hint in _proc_cmdline(pid):
            if best_named is None or depth > best_named[0]:
                best_named = (depth, pid)
        if best_euid is None or depth > best_euid[0]:
            best_euid = (depth, pid)

    if best_named is not None:
        return best_named[1]
    if best_euid is not None:
        return best_euid[1]
    return root_pid


# ---------- wake-file freshness ----------


def wake_file_age(
    mind: pathlib.Path,
    *,
    today: Optional[str] = None,
    now: Optional[float] = None,
) -> tuple[Optional[pathlib.Path], Optional[float]]:
    """Return ``(path, age_seconds)`` for the most recent wake file.

    Looks under ``mind/inner/thoughts/<today>/*.md``. ``today`` defaults
    to local-tz today (``YYYY-MM-DD``). Returns ``(None, None)`` if the
    directory is missing, empty, or only contains today's-not-written-yet
    files.

    The watchdog uses this as the heartbeat: ``wake.py`` writes the
    summary file at logical-work completion. If the process is alive
    but no file has been touched within M seconds, the wake is hung
    in cleanup (the SDK socket leak), not still working.
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
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
    # Anchor staleness to the live process's own start: if the newest
    # wake file we found predates *this* process's lifetime, it's a
    # carry-over from a prior thinker (container restart leaving the
    # bind-mounted /state/worker + today's thoughts dir intact, or a
    # leftover from several cadence cycles ago). Not evidence about
    # this process; treat as if it hasn't written anything yet and
    # fall through to the no-wake-file classifier, which already has
    # the right young-vs-old-process semantics.
    if wake_age is not None:
        proc_age = _proc_age_seconds(pid)
        if proc_age is not None and wake_age > proc_age:
            wake_age = None
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
    gone or if we lack permission — both are recoverable by letting the
    watchdog log and skip, rather than crashing the whole tick.

    ``PermissionError`` should not happen once :func:`find_signal_target`
    has resolved the alice-owned descendant; catching it here is a belt-
    and-suspenders diagnostic for the edge case where ``/proc`` visibility
    tightens enough that the resolver falls back to the root pid.
    """
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
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
        # kill() returned False for one of two reasons: the process is
        # already gone (the normal race window), or we lack permission
        # to signal it (:func:`_kill` catches PermissionError as a
        # recoverable case rather than crashing the tick). Check
        # proc_alive to tell them apart so the telemetry record is
        # accurate — the python pid resolver should make the permission
        # path effectively unreachable, but when it fires we want a
        # clean diagnostic rather than another "process exited" false
        # positive.
        if proc_alive(pid):
            return {
                "sigterm_sent": False,
                "sigterm_sufficient": False,
                "sigkill_sent": False,
                "elapsed_seconds": 0.0,
                "exited": False,
                "permission_denied": True,
            }
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

    pid = find_thinker_pid(lock_path)
    lock_exists = lock_path.exists()
    cadence = current_cadence_seconds(state_dir)
    threshold = stale_threshold_seconds(cadence)
    # Resolve the root (lock-holder) pid to the actual alice-owned
    # python worker pid. See :func:`find_signal_target`'s docstring for
    # the full rationale; the short version is that the lock holder is
    # root-owned (``flock`` runs before ``s6-setuidgid`` + ``timeout``
    # forks to uid=alice), so signaling it fails with PermissionError
    # on every intervention. ``target_pid`` is what flows into
    # ``is_stuck``'s staleness check, ``_read_wchan``, ``intervene``,
    # and the ``thinker_watchdog_intervention`` telemetry record.
    target_pid = find_signal_target(pid) if pid is not None else None
    wake_path, wake_age = wake_file_age(mind, today=today, now=now)
    decision = is_stuck(target_pid, wake_age, cadence, lock_exists=lock_exists)

    state = WatchdogState(
        decision=decision,
        pid=target_pid,
        cadence_seconds=cadence,
        stale_threshold_seconds=threshold,
        wake_file=wake_path,
        wake_file_age_seconds=wake_age,
        wchan=_read_wchan(target_pid) if target_pid is not None else None,
    )

    if decision == Decision.STUCK and target_pid is not None:
        timeline = intervene(target_pid)
        _append_event(
            events_log,
            {
                "pid": target_pid,
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
