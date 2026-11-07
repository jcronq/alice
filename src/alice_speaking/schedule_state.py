"""Active-cron state bridge for the status-site Schedules panel (#810).

Speaking is the only process with first-hand knowledge of which cron
jobs are currently armed (``CronCreate`` is a session-scoped tool).
The status-site container needs those rows to render the Schedules
panel, so Speaking keeps a sanitised JSON dump on disk that status-site
reads inline.

Writer contract
---------------

* Call :func:`record_cron` when ``CronCreate`` fires with an arg bundle
  the model emitted. We store ``id``, ``cron`` expression, optional
  ``description``, and timestamp fields. We NEVER store the ``prompt``
  arg — job prompts may contain internal notes (see the Phase 1
  boundary in the issue) and the status-site frontend has no business
  rendering them.
* Call :func:`forget_cron` when ``CronDelete`` fires (or the job's
  ``stop: true`` terminates a dynamic loop).
* Call :func:`update_run` when a cron fires: it bumps ``last_run_at``
  and ``last_run_outcome``, and recomputes ``next_run_at`` if the
  caller knows it.
* Call :func:`dump_snapshot` (or let :class:`ScheduleState` do it for
  you) to flush the in-memory registry to
  ``~/alice-mind/inner/state/active-crons.json`` atomically (tmp +
  rename). All mutators call :func:`dump_snapshot` by default so the
  file stays consistent with memory.

The writer side intentionally knows nothing about croniter; the status
site handles humanisation + countdown math. Keeping the dump focused
on raw fields means the humaniser can evolve without touching
Speaking.

Thread-safety
-------------

All mutators take a module-level :class:`threading.RLock`. Reads are
rare (:func:`dump_snapshot` only) so contention is negligible. The
lock also guards the on-disk file writes so concurrent cron tool
invocations don't race.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_LOCK = threading.RLock()

#: Keys that must never reach the on-disk snapshot. Mirrors
#: ``status_site.sources.schedules.FORBIDDEN_FIELDS`` — defense in
#: depth on both sides of the file boundary.
FORBIDDEN_KEYS: frozenset[str] = frozenset({"prompt", "prompt_preview"})


def active_crons_path(env: dict[str, str] | None = None) -> Path:
    """Resolve the on-disk snapshot path. Matches the reader contract
    in :mod:`status_site.sources.schedules`."""
    e = dict(os.environ if env is None else env)
    override = e.get("ACTIVE_CRONS_PATH")
    if override:
        return Path(override)
    mind = e.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    return Path(mind) / "inner" / "state" / "active-crons.json"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class ScheduledJob:
    """One active cron. Only safe fields — prompts live in-process only."""

    id: str
    cron: str
    description: str = ""
    created_at: str = ""
    last_run_at: str = ""
    last_run_outcome: str = ""  # "ok" | "error" | "" (never run)
    next_run_at: str = ""

    def sanitised(self) -> dict:
        """Shape written to disk. Explicit allowlist — if a future
        caller stuffs extra fields on the dataclass, they won't leak."""
        return {
            "id": self.id,
            "cron": self.cron,
            "description": self.description,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_run_outcome": self.last_run_outcome,
            "next_run_at": self.next_run_at,
        }


@dataclass
class ScheduleState:
    """In-memory registry of active cron jobs, mirrored to disk.

    One instance per process is enough; Speaking's daemon constructs
    it on startup and hands it to anywhere that needs to call the
    four mutators. Tests get their own instance pointed at a tmp path
    so parallel test runs don't trample each other.
    """

    path: Path = field(default_factory=active_crons_path)
    jobs: dict[str, ScheduledJob] = field(default_factory=dict)

    # ── mutators ────────────────────────────────────────────────

    def record_cron(
        self,
        *,
        id: str,
        cron: str,
        description: str = "",
        next_run_at: str = "",
        created_at: Optional[str] = None,
    ) -> ScheduledJob:
        """Register a new cron (or overwrite one with the same id).

        Fields not provided are left blank — the reader renders
        gracefully. Returns the stored record so callers can log.
        Calls :meth:`dump` so the on-disk snapshot stays current.
        """
        with _LOCK:
            job = ScheduledJob(
                id=str(id).strip(),
                cron=str(cron).strip(),
                description=str(description or "").strip(),
                created_at=created_at or _utc_now_iso(),
                next_run_at=str(next_run_at or ""),
            )
            if not job.id:
                raise ValueError("record_cron: id is required")
            self.jobs[job.id] = job
            self.dump()
            return job

    def forget_cron(self, job_id: str) -> bool:
        """Drop a cron by id. Returns ``True`` when it existed."""
        with _LOCK:
            removed = self.jobs.pop(str(job_id), None)
            if removed is not None:
                self.dump()
                return True
            return False

    def update_run(
        self,
        job_id: str,
        *,
        outcome: str,
        at: Optional[str] = None,
        next_run_at: Optional[str] = None,
    ) -> bool:
        """Record a cron firing.

        ``outcome`` is ``"ok"`` or ``"error"`` (other values pass
        through but the frontend only renders those two + unknown).
        ``at`` defaults to ``now``. ``next_run_at`` is optional —
        pass it when the caller knows the next fire time (e.g. after
        a ScheduleWakeup reply).

        Returns ``True`` when the job was known; ``False`` when it
        has already been forgotten (the race is harmless).
        """
        with _LOCK:
            job = self.jobs.get(str(job_id))
            if job is None:
                return False
            job.last_run_at = at or _utc_now_iso()
            job.last_run_outcome = str(outcome or "").strip()
            if next_run_at is not None:
                job.next_run_at = str(next_run_at)
            self.dump()
            return True

    def clear(self) -> None:
        """Reset the registry (used by tests + session resets)."""
        with _LOCK:
            self.jobs.clear()
            self.dump()

    # ── readers ─────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Serialisable view of current state — the exact JSON shape
        written to disk."""
        with _LOCK:
            return {
                "updated_at": _utc_now_iso(),
                "jobs": [j.sanitised() for j in self.jobs.values()],
            }

    # ── writer ──────────────────────────────────────────────────

    def dump(self) -> None:
        """Atomically overwrite the on-disk snapshot."""
        with _LOCK:
            payload = self.snapshot()
            _write_atomic(self.path, json.dumps(payload, indent=2, sort_keys=False))


# ─── module-level convenience ───────────────────────────────────────


_GLOBAL_STATE: ScheduleState | None = None


def global_state() -> ScheduleState:
    """Lazy process-wide instance. Callers that don't want to thread a
    state object through can use this; tests should construct their
    own ``ScheduleState(path=...)``."""
    global _GLOBAL_STATE
    with _LOCK:
        if _GLOBAL_STATE is None:
            _GLOBAL_STATE = ScheduleState()
        return _GLOBAL_STATE


def record_cron(**kwargs) -> ScheduledJob:
    """Module-level shim around :meth:`ScheduleState.record_cron`."""
    return global_state().record_cron(**kwargs)


def forget_cron(job_id: str) -> bool:
    """Module-level shim around :meth:`ScheduleState.forget_cron`."""
    return global_state().forget_cron(job_id)


def update_run(
    job_id: str,
    *,
    outcome: str,
    at: Optional[str] = None,
    next_run_at: Optional[str] = None,
) -> bool:
    """Module-level shim around :meth:`ScheduleState.update_run`."""
    return global_state().update_run(
        job_id, outcome=outcome, at=at, next_run_at=next_run_at
    )


def dump_snapshot() -> None:
    """Flush the global state to disk — useful on daemon shutdown."""
    global_state().dump()


# ─── helpers ────────────────────────────────────────────────────────


def _write_atomic(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` via tmp + rename.

    Creates parent dirs if missing — the status-site container may
    beat Speaking to the mount and we don't want a bind-mount dance
    to crash the first write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".tmp-{path.name}-{os.getpid()}-{time.time_ns()}"
    )
    tmp.write_text(body)
    os.replace(tmp, path)


def sanitise_cron_args(args: dict) -> dict:
    """Strip :data:`FORBIDDEN_KEYS` from a CronCreate arg bundle.

    Convenience for callers that want to log the full arg set (minus
    the prompt) somewhere else. Not called internally by the writer —
    :meth:`ScheduleState.record_cron` takes the allowlisted fields
    explicitly on purpose so new args don't accidentally become
    persistent state.
    """
    return {k: v for k, v in (args or {}).items() if k not in FORBIDDEN_KEYS}


__all__ = [
    "FORBIDDEN_KEYS",
    "ScheduleState",
    "ScheduledJob",
    "active_crons_path",
    "dump_snapshot",
    "forget_cron",
    "global_state",
    "record_cron",
    "sanitise_cron_args",
    "update_run",
]
