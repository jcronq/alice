"""Per-file flock guard for vault writes.

Shared infrastructure between :mod:`alice_thinking.wake` (the
generative hemisphere) and :mod:`alice_thinking.memory_worker` (the
maintenance loop). Both services mutate ``cortex-memory/*.md``; the
guard serializes those writes per file so a Stage C atomize can't
collide with a thinking-side append to the same note.

Design contract — see
``cortex-memory/research/2026-06-01-memory-worker-extraction-design.md``
§6 (Crash recovery: lock + journal).

Phase 1 only wires the exclusive-write path (``LockMode.EXCLUSIVE``).
The shared-read path (``LockMode.SHARED``) is included in the API so
later phases can opt into a read lock when the read needs to be a
consistent snapshot of an in-flight write — the underlying
``fcntl.flock`` call already supports ``LOCK_SH``.

Concurrency notes
-----------------

* The lock file lives **next to** the target — same directory,
  ``.<name>.lock`` so we don't try to flock the markdown file itself
  (flocking the same fd we're about to truncate is portable but
  surprising; a sidecar avoids the foot-gun and lets readers acquire
  ``LOCK_SH`` without opening the live file).
* ``flock`` is advisory and process-scoped. Threads inside one
  process share the fd's lock state, which is what we want: thinking
  + memory-worker run as separate processes, so process-scoped is
  exactly the boundary that matters. Threads within a single service
  must coordinate via in-process locks if they care; this module
  does not promise per-thread mutual exclusion.
* ``LockMode.EXCLUSIVE`` blocks by default; pass ``timeout`` to fall
  back to ``LOCK_NB`` with a poll loop so the supervisor can decide
  whether to skip the file or retry on the next cycle.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import fcntl
import os
import pathlib
import time
from typing import Iterator


class LockMode(enum.Enum):
    """Lock acquisition mode.

    ``EXCLUSIVE`` maps to :data:`fcntl.LOCK_EX` — only one holder at
    a time, blocks readers and other writers.

    ``SHARED`` maps to :data:`fcntl.LOCK_SH` — multiple readers may
    hold the lock concurrently; an exclusive writer blocks until all
    readers release.
    """

    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class VaultLockTimeout(TimeoutError):
    """Raised when a non-blocking acquisition fails before ``timeout`` elapses.

    Callers in the memory worker treat this as "skip this file, try
    again next cycle" rather than a hard failure.
    """


def _flock_op(mode: LockMode, *, blocking: bool) -> int:
    """Translate :class:`LockMode` + blocking flag into a flock op int."""
    base = fcntl.LOCK_EX if mode is LockMode.EXCLUSIVE else fcntl.LOCK_SH
    if not blocking:
        base |= fcntl.LOCK_NB
    return base


def _sidecar_path(target: pathlib.Path) -> pathlib.Path:
    """Return the lock-file path that pairs with ``target``.

    The sidecar lives in the same directory as the target, prefixed
    with ``.`` and suffixed with ``.lock`` so directory listings and
    wikilink scans (which already skip dotfiles) don't pick it up.
    """
    return target.parent / f".{target.name}.lock"


@contextlib.contextmanager
def acquire(
    target: pathlib.Path,
    *,
    mode: LockMode = LockMode.EXCLUSIVE,
    timeout: float | None = None,
    poll_interval: float = 0.05,
) -> Iterator[pathlib.Path]:
    """Context manager that holds an advisory lock on ``target``.

    Parameters
    ----------
    target
        Path the caller intends to read or write. Need not exist
        yet — the lock attaches to a sidecar so writes can create
        the target inside the critical section.
    mode
        :data:`LockMode.EXCLUSIVE` (default) for writes,
        :data:`LockMode.SHARED` for consistent-snapshot reads.
    timeout
        ``None`` (default) blocks until the lock is granted.
        ``0`` raises :class:`VaultLockTimeout` immediately if the
        lock is held. ``> 0`` polls with ``LOCK_NB`` every
        ``poll_interval`` seconds and raises after the deadline.
    poll_interval
        Seconds between non-blocking retries when ``timeout`` is
        positive. Default ``0.05`` (50 ms) — small enough to feel
        instantaneous, large enough to not spin.

    Yields
    ------
    pathlib.Path
        The sidecar lock path actually held. Mostly useful for
        diagnostics / logging.

    Raises
    ------
    VaultLockTimeout
        Non-blocking acquisition exceeded ``timeout``.
    """
    sidecar = _sidecar_path(target)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    # Open RW so SHARED holders and EXCLUSIVE holders open the same
    # access mode; otherwise some platforms refuse LOCK_EX on a
    # read-only fd.
    fd = os.open(sidecar, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if timeout is None:
            fcntl.flock(fd, _flock_op(mode, blocking=True))
        else:
            deadline = time.monotonic() + max(timeout, 0.0)
            op = _flock_op(mode, blocking=False)
            while True:
                try:
                    fcntl.flock(fd, op)
                    break
                except BlockingIOError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    if time.monotonic() >= deadline:
                        raise VaultLockTimeout(
                            f"could not acquire {mode.value} lock on "
                            f"{target} within {timeout}s"
                        ) from exc
                    time.sleep(poll_interval)
        try:
            yield sidecar
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Unlock-on-close is implicit; swallowing here keeps
                # cleanup quiet if the fd was already revoked (e.g.
                # the sidecar got unlinked under us by a janitor).
                pass
    finally:
        os.close(fd)
