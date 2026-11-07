"""Thinker watchdog — deterministic floor for hung-wake recovery.

Each rule branch lives behind a pure function in
``alice_thinking.watchdog``: PID discovery, wake-file freshness,
cadence read, decision rules, intervention escalation, and
events.jsonl emission. These tests pin the rules without touching
real processes or the host filesystem.

Spec: ``docs/designs/thinker-watchdog.md`` plus the design note
"thinking watchdog phase 1" in
``cortex-memory/research/2026-05-08-thinker-watchdog-design.md``
(committed alongside the implementation PR).
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import pytest

from alice_thinking import watchdog as wd


# ---------- helpers ----------


def _make_lock(tmp_path: pathlib.Path) -> pathlib.Path:
    lock = tmp_path / "thinking.lock"
    lock.touch()
    return lock


def _make_state_dir(
    tmp_path: pathlib.Path, *, cadence: Optional[int] = None
) -> pathlib.Path:
    state = tmp_path / "state"
    state.mkdir()
    if cadence is not None:
        (state / wd.INTERVAL_FILE_NAME).write_text(f"{cadence}\n")
    return state


def _make_mind(
    tmp_path: pathlib.Path,
    *,
    today: Optional[str] = None,
    wake_age_seconds: Optional[float] = None,
) -> pathlib.Path:
    mind = tmp_path / "mind"
    (mind / "memory").mkdir(parents=True)
    if wake_age_seconds is not None:
        if today is None:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day = mind / "inner" / "thoughts" / today
        day.mkdir(parents=True)
        wake = day / "120000-wake.md"
        wake.write_text("---\nmode: active\ndid_work: true\n---\nbody\n")
        target_mtime = time.time() - wake_age_seconds
        import os

        os.utime(wake, (target_mtime, target_mtime))
    return mind


def _locks_text(*, pid: int, inode: int) -> str:
    """Synthesize one ``/proc/locks`` line that holds the given inode."""
    return f"1: FLOCK  ADVISORY  WRITE {pid} fd:00:{inode} 0 EOF\n"


# ---------- find_thinker_pid ----------


def test_find_thinker_pid_returns_none_when_no_lock_file(tmp_path):
    assert wd.find_thinker_pid(tmp_path / "missing") is None


def test_find_thinker_pid_returns_none_when_no_holder(tmp_path):
    lock = _make_lock(tmp_path)
    # Empty /proc/locks — file exists but nobody holds an flock on it.
    assert wd.find_thinker_pid(lock, locks_text="") is None


def test_find_thinker_pid_matches_inode(tmp_path):
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    text = _locks_text(pid=4242, inode=inode)
    assert wd.find_thinker_pid(lock, locks_text=text) == 4242


def test_find_thinker_pid_ignores_wrong_inode(tmp_path):
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    text = _locks_text(pid=4242, inode=inode + 999)
    assert wd.find_thinker_pid(lock, locks_text=text) is None


def test_find_thinker_pid_ignores_posix_locks(tmp_path):
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    text = f"1: POSIX  ADVISORY  WRITE 4242 fd:00:{inode} 0 EOF\n"
    assert wd.find_thinker_pid(lock, locks_text=text) is None


def test_find_thinker_pid_handles_malformed_lines(tmp_path):
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    text = (
        "garbage\n"
        f"1: FLOCK  ADVISORY  WRITE notapid fd:00:{inode} 0 EOF\n"
        f"2: FLOCK  ADVISORY  WRITE 4242 fd:00:{inode} 0 EOF\n"
    )
    assert wd.find_thinker_pid(lock, locks_text=text) == 4242


# ---------- _resolve_alice_child_pid ----------


def _uid_line(euid: int) -> str:
    """Synthesize a ``/proc/<pid>/status`` ``Uid:`` field."""
    return f"{euid}\t{euid}\t{euid}\t{euid}"


def test_resolve_alice_child_pid_returns_wrapper_if_already_alice(monkeypatch):
    """Wrapper itself runs as alice (e.g. s6 service drops privs
    earlier in the chain) — no descendant walk needed."""
    procs = {100: {"PPid": "1", "Uid": _uid_line(1000)}}
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    assert wd._resolve_alice_child_pid(100, our_euid=1000) == 100


def test_resolve_alice_child_pid_walks_to_alice_descendant(monkeypatch):
    """Realistic s6 chain: root flock → alice-owned python. The
    watchdog must signal the python, not the root wrapper."""
    procs = {
        100: {"PPid": "1", "Uid": _uid_line(0)},        # flock, root
        200: {"PPid": "100", "Uid": _uid_line(1000)},   # python, alice
    }
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    assert wd._resolve_alice_child_pid(100, our_euid=1000) == 200


def test_resolve_alice_child_pid_walks_multi_hop(monkeypatch):
    """flock(root) → s6-setuidgid(root exec'd into alice) → alice-think
    → timeout → python. BFS must traverse multiple generations when
    the direct children are still root-owned (e.g. intermediate shims)."""
    procs = {
        100: {"PPid": "1", "Uid": _uid_line(0)},         # flock, root
        200: {"PPid": "100", "Uid": _uid_line(0)},       # shim, root
        300: {"PPid": "200", "Uid": _uid_line(1000)},    # python, alice
    }
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    assert wd._resolve_alice_child_pid(100, our_euid=1000) == 300


def test_resolve_alice_child_pid_none_when_no_alice_descendant(monkeypatch):
    """Orphan root wrapper (python already exited) — refuse to signal
    root; return None and let the caller log a warning."""
    procs = {100: {"PPid": "1", "Uid": _uid_line(0)}}
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    assert wd._resolve_alice_child_pid(100, our_euid=1000) is None


def test_resolve_alice_child_pid_none_when_wrapper_gone(monkeypatch):
    """Race: /proc/<wrapper_pid> vanished between the lock read and
    the resolver — return None rather than fall through to signalling
    a stale PID."""
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: {})
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: [])
    assert wd._resolve_alice_child_pid(100, our_euid=1000) is None


def test_resolve_alice_child_pid_ignores_siblings(monkeypatch):
    """Other alice processes elsewhere on the system must NOT match —
    the resolver only returns descendants of the specific wrapper."""
    procs = {
        100: {"PPid": "1", "Uid": _uid_line(0)},         # our wrapper
        200: {"PPid": "100", "Uid": _uid_line(1000)},    # our target
        500: {"PPid": "1", "Uid": _uid_line(1000)},      # sibling alice proc
        600: {"PPid": "500", "Uid": _uid_line(1000)},    # sibling's child
    }
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    assert wd._resolve_alice_child_pid(100, our_euid=1000) == 200


def test_run_tick_resolves_pid_before_intervention(tmp_path, monkeypatch):
    """End-to-end wiring: /proc/locks reports the root wrapper, but
    run_tick substitutes the alice-owned descendant before any
    intervention or logging reads /proc/<pid>/wchan."""
    today = "2026-05-08"
    state = _make_state_dir(tmp_path, cadence=300)
    mind = _make_mind(tmp_path, today=today, wake_age_seconds=1700)
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    wrapper_pid = 100
    child_pid = 200
    procs = {
        wrapper_pid: {"PPid": "1", "Uid": _uid_line(0)},
        child_pid: {"PPid": str(wrapper_pid), "Uid": _uid_line(1000)},
    }
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [(wrapper_pid, inode)])
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    monkeypatch.setattr(wd, "_read_wchan", lambda pid: "ep_poll" if pid == child_pid else None)
    captured: dict[str, int] = {}

    def fake_intervene(pid, **kw):  # noqa: ARG001
        captured["pid"] = pid
        return {
            "sigterm_sent": True,
            "sigterm_sufficient": True,
            "sigkill_sent": False,
            "elapsed_seconds": 0.1,
            "exited": True,
        }

    monkeypatch.setattr(wd, "intervene", fake_intervene)

    # Force the resolver's "our" euid to 1000 regardless of the test
    # runner's actual uid (CI containers run as root, local dev may
    # vary) — the resolver's job is "match my euid", so freezing it
    # keeps the test deterministic.
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    result = wd.run_tick(
        lock_path=lock,
        state_dir=state,
        mind=mind,
        events_log=mind / "memory" / "events.jsonl",
        today=today,
    )
    assert result.decision == wd.Decision.STUCK
    assert result.pid == child_pid  # resolved, not the root wrapper
    assert captured["pid"] == child_pid
    assert result.extra.get("wrapper_pid") == wrapper_pid


def test_run_tick_no_alice_descendant_does_not_intervene(tmp_path, monkeypatch):
    """When the wrapper is live but no alice-owned child exists, the
    watchdog must NOT try to intervene (kill root → EPERM, pollutes
    logs). The tick logs a warning and classifies as idle."""
    today = "2026-05-08"
    state = _make_state_dir(tmp_path, cadence=300)
    # Wake file is stale — pre-fix, this would trip STUCK and SIGTERM
    # the root wrapper.
    mind = _make_mind(tmp_path, today=today, wake_age_seconds=1700)
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    wrapper_pid = 100
    procs = {wrapper_pid: {"PPid": "1", "Uid": _uid_line(0)}}
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [(wrapper_pid, inode)])
    monkeypatch.setattr(wd, "_read_proc_status", lambda pid: procs.get(pid, {}))
    monkeypatch.setattr(wd, "_list_proc_pids", lambda: list(procs))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    called = {"n": 0}

    def _bang(*_a, **_k):
        called["n"] += 1
        raise AssertionError("intervene must not run without alice-owned child")

    monkeypatch.setattr(wd, "intervene", _bang)

    result = wd.run_tick(
        lock_path=lock,
        state_dir=state,
        mind=mind,
        events_log=mind / "memory" / "events.jsonl",
        today=today,
    )
    # pid resolved to None → ORPHAN_LOCK (lock exists, no live holder
    # we can act on). Intervention is skipped.
    assert result.pid is None
    assert called["n"] == 0


# ---------- wake_file_age ----------


def test_wake_file_age_returns_none_when_no_thoughts_dir(tmp_path):
    mind = tmp_path / "mind"
    mind.mkdir()
    p, age = wd.wake_file_age(mind)
    assert p is None
    assert age is None


def test_wake_file_age_default_today_is_utc(tmp_path, monkeypatch):
    """Regression for #785 bug 2 (UTC/local skew).

    ``wake.py`` writes to ``inner/thoughts/<today-UTC>/`` per the
    prelude convention (``date -u +%Y-%m-%d``). The watchdog must
    use the same UTC key. Pre-fix used local time; a container
    booted after UTC midnight but before local midnight (EST: local
    ~19:00-23:59) would skew — local-today's dir still holds
    yesterday-UTC's old wake files, tripping a false-positive STUCK.
    """
    # Simulate "local Nov 6, UTC Nov 7" — the exact skew window.
    class _FakeDT:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 11, 6, 23, 30, 0)  # local, naive
            return datetime(2026, 11, 7, 4, 30, 0, tzinfo=tz)  # UTC side

        @classmethod
        def strftime(cls, fmt):  # pragma: no cover — compat shim
            return cls.now().strftime(fmt)

    monkeypatch.setattr(wd, "datetime", _FakeDT)

    # Only the UTC-dated dir has today's wake files. The local-dated
    # dir has stale files from "yesterday" (which the pre-fix watchdog
    # would wrongly read).
    utc_today = "2026-11-07"
    local_today = "2026-11-06"
    utc_dir = tmp_path / "inner" / "thoughts" / utc_today
    utc_dir.mkdir(parents=True)
    fresh = utc_dir / "040000-wake.md"
    fresh.write_text("fresh")
    fresh_mtime = time.time() - 60  # 1 min old
    os.utime(fresh, (fresh_mtime, fresh_mtime))

    local_dir = tmp_path / "inner" / "thoughts" / local_today
    local_dir.mkdir(parents=True)
    stale = local_dir / "180000-wake.md"
    stale.write_text("stale")
    stale_mtime = time.time() - 5 * 3600  # 5h old → would trip STUCK
    os.utime(stale, (stale_mtime, stale_mtime))

    # ``now`` is still real time (seconds since epoch), so ``age`` uses
    # the real clock — only the date-key defaults come from the fake.
    p, age = wd.wake_file_age(tmp_path)
    assert p == fresh, "watchdog must read UTC-today, not local-today"
    assert age is not None and age < 120


def test_wake_file_age_picks_newest(tmp_path):
    today = "2026-05-08"
    mind = tmp_path / "mind"
    day = mind / "inner" / "thoughts" / today
    day.mkdir(parents=True)
    older = day / "100000-wake.md"
    older.write_text("a")
    newer = day / "110000-wake.md"
    newer.write_text("b")
    import os

    now = time.time()
    os.utime(older, (now - 600, now - 600))
    os.utime(newer, (now - 30, now - 30))
    p, age = wd.wake_file_age(mind, today=today, now=now)
    assert p == newer
    assert age == pytest.approx(30, abs=1)


# ---------- current_cadence_seconds + stale threshold ----------


def test_cadence_read_from_state_file_with_fallback(tmp_path):
    # Exists + parseable.
    state = _make_state_dir(tmp_path, cadence=900)
    assert wd.current_cadence_seconds(state) == 900

    # Malformed → fallback.
    (state / wd.INTERVAL_FILE_NAME).write_text("not-an-int\n")
    assert wd.current_cadence_seconds(state) == wd.DEFAULT_CADENCE_SECONDS

    # Missing entirely → fallback.
    (state / wd.INTERVAL_FILE_NAME).unlink()
    assert wd.current_cadence_seconds(state) == wd.DEFAULT_CADENCE_SECONDS

    # Zero / negative / empty → fallback (defensive: don't pick a
    # threshold of 60s and start murdering healthy wakes).
    (state / wd.INTERVAL_FILE_NAME).write_text("0\n")
    assert wd.current_cadence_seconds(state) == wd.DEFAULT_CADENCE_SECONDS
    (state / wd.INTERVAL_FILE_NAME).write_text("\n")
    assert wd.current_cadence_seconds(state) == wd.DEFAULT_CADENCE_SECONDS


def test_stale_threshold_math():
    # 5x cadence + 60s cushion. Sized for native Stage B workflow runs
    # (LLM subroutines push the legitimate-completion window to ~20 min).
    assert wd.stale_threshold_seconds(300) == 1560  # 5min cadence → 26 min
    assert wd.stale_threshold_seconds(2400) == 12060  # 40min cadence → 201 min
    assert wd.stale_threshold_seconds(600) == 3060  # 10min fallback → 51 min


def test_stale_threshold_covers_native_workflow_duration():
    """Regression guard: a native Stage B workflow with LLM-subroutine
    fan-out can legitimately run 15–20 minutes. Active-mode threshold
    must be at least 20 minutes so a healthy long workflow isn't
    SIGTERMed mid-flight.

    Prior 2x multiplier (660s = 11 min) was the original bug — see PR
    #21 follow-up commit.
    """
    workflow_worst_case_seconds = 20 * 60  # 20 minutes
    active_cadence = 300
    threshold = wd.stale_threshold_seconds(active_cadence)
    assert threshold > workflow_worst_case_seconds, (
        f"threshold {threshold}s must exceed workflow worst-case "
        f"{workflow_worst_case_seconds}s to avoid false-positive kills"
    )


# ---------- is_stuck rule branches ----------


def test_no_process_no_lock_returns_idle():
    assert (
        wd.is_stuck(pid=None, wake_age=None, cadence=300, lock_exists=False)
        == wd.Decision.IDLE
    )


def test_process_alive_recent_wake_returns_working():
    assert (
        wd.is_stuck(pid=4242, wake_age=10.0, cadence=300, lock_exists=True)
        == wd.Decision.WORKING
    )


@pytest.mark.parametrize(
    "cadence,wake_age",
    [
        (300, 1700),  # active mode (5min) past the 1560s threshold
        (600, 3200),  # 10min cadence past the 3060s threshold
        (2400, 13000),  # sleep@40min past the 12060s threshold
    ],
)
def test_process_alive_stale_wake_returns_stuck(cadence, wake_age, monkeypatch):
    # The rule itself doesn't read /proc — only is_stuck's no-wake-file
    # branch does. Pass an explicit wake_age to keep this pure.
    decision = wd.is_stuck(
        pid=4242, wake_age=wake_age, cadence=cadence, lock_exists=True
    )
    assert decision == wd.Decision.STUCK


def test_no_process_with_lock_returns_orphan_lock():
    decision = wd.is_stuck(pid=None, wake_age=None, cadence=300, lock_exists=True)
    assert decision == wd.Decision.ORPHAN_LOCK


def test_no_wake_file_with_young_process_is_working(monkeypatch):
    # Process started 5s ago, threshold is 1560s → still working.
    monkeypatch.setattr(wd, "_proc_age_seconds", lambda pid, now=None: 5.0)
    decision = wd.is_stuck(pid=4242, wake_age=None, cadence=300, lock_exists=True)
    assert decision == wd.Decision.WORKING


def test_no_wake_file_with_old_process_is_stuck(monkeypatch):
    # Process started 35min ago, no wake file at all → stuck.
    monkeypatch.setattr(wd, "_proc_age_seconds", lambda pid, now=None: 35 * 60)
    decision = wd.is_stuck(pid=4242, wake_age=None, cadence=300, lock_exists=True)
    assert decision == wd.Decision.STUCK


# ---------- intervene ----------


class _FakeProc:
    """Test double for the kill+poll loop. Models a process that exits
    after a configurable number of polls following the *first* signal."""

    def __init__(self, *, exits_after_term: bool, exits_after_kill: bool = True):
        self.alive = True
        self.signals: list[int] = []
        self._term_seen = False
        self._kill_seen = False
        self._exits_after_term = exits_after_term
        self._exits_after_kill = exits_after_kill
        self._fake_now = 0.0

    def kill(self, pid: int, sig: int) -> bool:  # noqa: ARG002
        if not self.alive:
            return False
        self.signals.append(sig)
        if sig == signal.SIGTERM:
            self._term_seen = True
        elif sig == signal.SIGKILL:
            self._kill_seen = True
        return True

    def proc_alive(self, pid: int) -> bool:  # noqa: ARG002
        return self.alive

    def sleep(self, seconds: float) -> None:
        # Advance our fake clock; flip alive→False once the right
        # signal has had a tick to land.
        self._fake_now += seconds
        if self._term_seen and self._exits_after_term and self.alive:
            self.alive = False
        elif self._kill_seen and self._exits_after_kill and self.alive:
            self.alive = False

    def now(self) -> float:
        return self._fake_now


def test_intervene_sigterm_sufficient():
    fake = _FakeProc(exits_after_term=True)
    result = wd.intervene(
        4242,
        grace_seconds=5,
        kill=fake.kill,
        proc_alive=fake.proc_alive,
        sleep=fake.sleep,
        now=fake.now,
    )
    assert result["sigterm_sent"] is True
    assert result["sigterm_sufficient"] is True
    assert result["sigkill_sent"] is False
    assert result["exited"] is True
    assert fake.signals == [signal.SIGTERM]


def test_intervene_falls_back_to_sigkill():
    fake = _FakeProc(exits_after_term=False, exits_after_kill=True)
    result = wd.intervene(
        4242,
        grace_seconds=2,
        kill=fake.kill,
        proc_alive=fake.proc_alive,
        sleep=fake.sleep,
        now=fake.now,
    )
    assert result["sigterm_sent"] is True
    assert result["sigterm_sufficient"] is False
    assert result["sigkill_sent"] is True
    assert result["exited"] is True
    assert signal.SIGTERM in fake.signals
    assert signal.SIGKILL in fake.signals


def test_intervene_handles_already_dead_process():
    """If the process exits between the watchdog's decision and the
    SIGTERM, ProcessLookupError shouldn't crash the tick — we just
    record an empty intervention and move on."""

    def kill(pid, sig):  # noqa: ARG001
        return False

    def proc_alive(pid):  # noqa: ARG001
        return False

    result = wd.intervene(
        4242,
        grace_seconds=2,
        kill=kill,
        proc_alive=proc_alive,
        sleep=lambda s: None,
        now=time.time,
    )
    assert result["sigterm_sent"] is False
    assert result["sigterm_sufficient"] is True
    assert result["exited"] is True


# ---------- run_tick + telemetry ----------


def _read_events(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_run_tick_idle_no_events(tmp_path, monkeypatch):
    state = _make_state_dir(tmp_path, cadence=300)
    mind = _make_mind(tmp_path)
    lock = tmp_path / "no-such-lock"
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [])
    result = wd.run_tick(
        lock_path=lock,
        state_dir=state,
        mind=mind,
        events_log=mind / "memory" / "events.jsonl",
    )
    assert result.decision == wd.Decision.IDLE
    assert _read_events(mind / "memory" / "events.jsonl") == []


def test_run_tick_orphan_lock_clears_and_emits(tmp_path, monkeypatch):
    state = _make_state_dir(tmp_path, cadence=300)
    mind = _make_mind(tmp_path)
    lock = _make_lock(tmp_path)
    # Lock file exists but no holder in /proc/locks → orphan.
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [])

    events_log = mind / "memory" / "events.jsonl"
    result = wd.run_tick(
        lock_path=lock, state_dir=state, mind=mind, events_log=events_log
    )

    assert result.decision == wd.Decision.ORPHAN_LOCK
    assert not lock.exists()  # cleared
    events = _read_events(events_log)
    assert len(events) == 1
    assert events[0]["event"] == "thinker_watchdog_orphan_lock"
    assert events[0]["lock_path"] == str(lock)


def test_telemetry_event_emitted_on_intervention(tmp_path, monkeypatch):
    """The full happy-path: lock held, wake file is stale, watchdog
    SIGTERMs, the (mocked) process exits, one record lands in
    events.jsonl with the expected schema."""
    today = "2026-05-08"
    state = _make_state_dir(tmp_path, cadence=300)
    mind = _make_mind(tmp_path, today=today, wake_age_seconds=1700)  # > 1560s threshold
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    fake_pid = 12345

    # Synthesize /proc/locks to claim our fake_pid holds the lock.
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [(fake_pid, inode)])
    # Bypass the alice-descendant resolver — the fake pid has no /proc
    # entry, so the real resolver would return None. The existing
    # telemetry schema contract is independent of pid resolution.
    monkeypatch.setattr(wd, "_resolve_alice_child_pid", lambda pid, **kw: pid)
    # /proc/<pid>/wchan reads — simulate the SDK epoll leak signature.
    monkeypatch.setattr(wd, "_read_wchan", lambda pid: "ep_poll")

    # Bypass the real os.kill / time.sleep loop with a fixed payload —
    # intervene() itself is exercised by its own tests above; here we
    # just need run_tick to emit the event with the right schema.
    monkeypatch.setattr(
        wd,
        "intervene",
        lambda pid, **kw: {
            "sigterm_sent": True,
            "sigterm_sufficient": True,
            "sigkill_sent": False,
            "elapsed_seconds": 1.0,
            "exited": True,
        },
    )

    events_log = mind / "memory" / "events.jsonl"
    result = wd.run_tick(
        lock_path=lock,
        state_dir=state,
        mind=mind,
        events_log=events_log,
        today=today,
    )

    assert result.decision == wd.Decision.STUCK
    assert result.pid == fake_pid
    assert result.wchan == "ep_poll"

    events = _read_events(events_log)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "thinker_watchdog_intervention"
    assert e["pid"] == fake_pid
    assert e["wchan"] == "ep_poll"
    assert e["cadence_seconds"] == 300
    assert e["stale_threshold_seconds"] == 1560
    assert e["wake_file_age_seconds"] >= 1560
    assert e["sigterm_sent"] is True
    assert e["sigterm_sufficient"] is True


def test_fresh_thinker_not_stuck_within_threshold(tmp_path, monkeypatch):
    """Regression for #785 bug 2 live-site symptom.

    Fresh container boot shortly after UTC midnight (EST evening):
    thinker supervisor just started, python child has been running
    maybe 30 seconds, and the only wake file on disk is from the
    previous local-day sitting in ``inner/thoughts/<local-today>/``.
    Pre-fix, the watchdog read local-today's dir, computed wake_age
    = several hours, and declared STUCK within 3 minutes.

    With the UTC fix, the wrong-dir read goes away: watchdog reads
    ``<utc-today>/`` which is empty (or has the very recent fresh
    wake), falls through to ``_classify_no_wake_file`` with a small
    process age, and correctly returns WORKING.
    """
    # Simulate "local Nov 6, UTC Nov 7" — same skew window as the report.
    class _FakeDT:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 11, 6, 23, 30, 0)
            return datetime(2026, 11, 7, 4, 30, 0, tzinfo=tz)

    monkeypatch.setattr(wd, "datetime", _FakeDT)

    state = _make_state_dir(tmp_path, cadence=300)
    # Build the mind with stale wake files on local-today's side (the
    # dir the pre-fix watchdog would read). UTC-today's dir is empty
    # because the fresh thinker hasn't written its first file yet.
    mind = tmp_path / "mind"
    (mind / "memory").mkdir(parents=True)
    local_dir = mind / "inner" / "thoughts" / "2026-11-06"
    local_dir.mkdir(parents=True)
    stale = local_dir / "180000-wake.md"
    stale.write_text("stale")
    stale_mtime = time.time() - 5 * 3600
    os.utime(stale, (stale_mtime, stale_mtime))

    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    fake_pid = 12345
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [(fake_pid, inode)])
    monkeypatch.setattr(wd, "_resolve_alice_child_pid", lambda pid, **kw: pid)
    monkeypatch.setattr(wd, "_read_wchan", lambda pid: None)
    # Freshly-started thinker: 30s of wall-clock, well under the
    # 1560s threshold for a 300s cadence.
    monkeypatch.setattr(wd, "_proc_age_seconds", lambda pid, now=None: 30.0)

    def _bang(*_a, **_k):
        raise AssertionError("intervene must not run for a fresh thinker")

    monkeypatch.setattr(wd, "intervene", _bang)

    result = wd.run_tick(
        lock_path=lock,
        state_dir=state,
        mind=mind,
        events_log=mind / "memory" / "events.jsonl",
    )
    assert result.decision == wd.Decision.WORKING
    assert result.wake_file is None  # UTC-today dir is empty, correctly


def test_run_tick_working_no_events(tmp_path, monkeypatch):
    today = "2026-05-08"
    state = _make_state_dir(tmp_path, cadence=300)
    mind = _make_mind(tmp_path, today=today, wake_age_seconds=10)
    lock = _make_lock(tmp_path)
    inode = lock.stat().st_ino
    monkeypatch.setattr(wd, "_read_proc_locks", lambda: [(99999, inode)])
    monkeypatch.setattr(wd, "_resolve_alice_child_pid", lambda pid, **kw: pid)
    monkeypatch.setattr(wd, "_read_wchan", lambda pid: "do_select")

    events_log = mind / "memory" / "events.jsonl"
    result = wd.run_tick(
        lock_path=lock,
        state_dir=state,
        mind=mind,
        events_log=events_log,
        today=today,
    )
    assert result.decision == wd.Decision.WORKING
    assert _read_events(events_log) == []
