"""Tests for :mod:`alice_thinking.vault_lock`.

Covers acquire/release, exclusive-vs-shared semantics, timeout
behavior, and inter-process contention via :mod:`multiprocessing`.
The contention tests are the load-bearing ones — the locking story
only matters if two processes can actually serialize on it.
"""

from __future__ import annotations

import multiprocessing as mp
import pathlib
import threading
import time

import pytest

from alice_thinking.vault_lock import (
    LockMode,
    VaultLockTimeout,
    acquire,
)


# ---------- happy path ----------


def test_acquire_release_exclusive_creates_sidecar(tmp_path: pathlib.Path) -> None:
    """Exclusive acquire on a non-existent target still creates the sidecar."""
    target = tmp_path / "note.md"  # target need not exist
    with acquire(target, mode=LockMode.EXCLUSIVE) as sidecar:
        assert sidecar == tmp_path / ".note.md.lock"
        assert sidecar.is_file()
    # Sidecar persists after release — flock is advisory, the file is
    # the lock anchor. No cleanup contract for it.
    assert sidecar.is_file()


def test_acquire_release_shared(tmp_path: pathlib.Path) -> None:
    """Shared acquire on an empty path works."""
    target = tmp_path / "note.md"
    with acquire(target, mode=LockMode.SHARED) as sidecar:
        assert sidecar.is_file()


def test_two_shared_acquires_in_same_process_do_not_block(
    tmp_path: pathlib.Path,
) -> None:
    """LOCK_SH allows multiple holders; same-process nested SH acquire
    must not deadlock."""
    target = tmp_path / "note.md"
    with acquire(target, mode=LockMode.SHARED):
        with acquire(target, mode=LockMode.SHARED, timeout=1.0):
            pass


# ---------- timeout behavior ----------


def test_exclusive_timeout_zero_raises_when_held(
    tmp_path: pathlib.Path,
) -> None:
    """timeout=0 is fail-fast: held lock → immediate VaultLockTimeout.

    We hold the first lock in a worker thread because flock is
    process-scoped and same-process re-acquisition of LOCK_EX on the
    same fd is not guaranteed to block on Linux. A second process
    (or thread holding its own fd) is the realistic test.
    """
    target = tmp_path / "note.md"
    holder_acquired = threading.Event()
    holder_release = threading.Event()

    def hold_lock() -> None:
        with acquire(target, mode=LockMode.EXCLUSIVE):
            holder_acquired.set()
            holder_release.wait(timeout=5.0)

    t = threading.Thread(target=hold_lock)
    t.start()
    try:
        assert holder_acquired.wait(timeout=5.0)
        with pytest.raises(VaultLockTimeout):
            with acquire(target, mode=LockMode.EXCLUSIVE, timeout=0.0):
                pass
    finally:
        holder_release.set()
        t.join(timeout=5.0)


def test_exclusive_timeout_positive_raises_after_deadline(
    tmp_path: pathlib.Path,
) -> None:
    """timeout=0.2 must raise within a reasonable window when the lock
    is held by another thread for longer than that."""
    target = tmp_path / "note.md"
    holder_acquired = threading.Event()
    holder_release = threading.Event()

    def hold_lock() -> None:
        with acquire(target, mode=LockMode.EXCLUSIVE):
            holder_acquired.set()
            holder_release.wait(timeout=5.0)

    t = threading.Thread(target=hold_lock)
    t.start()
    try:
        assert holder_acquired.wait(timeout=5.0)
        start = time.monotonic()
        with pytest.raises(VaultLockTimeout):
            with acquire(target, mode=LockMode.EXCLUSIVE, timeout=0.2):
                pass
        # Allow generous slack (CI under load can be slow); the
        # invariant is "raises before infinity", not micro-precision.
        elapsed = time.monotonic() - start
        assert 0.15 <= elapsed < 2.0
    finally:
        holder_release.set()
        t.join(timeout=5.0)


# ---------- inter-process contention ----------


def _hold_lock_child(
    target_str: str,
    acquired_path_str: str,
    release_path_str: str,
) -> None:
    """Child-process helper: acquire, signal via file sentinel, wait."""
    # Re-import inside the child so spawn-mode multiprocessing
    # (the macOS default) picks the module up correctly.
    from alice_thinking.vault_lock import LockMode as _LockMode
    from alice_thinking.vault_lock import acquire as _acquire

    target = pathlib.Path(target_str)
    acquired = pathlib.Path(acquired_path_str)
    release = pathlib.Path(release_path_str)
    with _acquire(target, mode=_LockMode.EXCLUSIVE):
        acquired.touch()
        # Bounded wait so a buggy test can't hang CI forever.
        deadline = time.monotonic() + 10.0
        while not release.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)


def test_exclusive_lock_blocks_across_processes(
    tmp_path: pathlib.Path,
) -> None:
    """The real contract: two processes serialize on the lock.

    We use a file-sentinel handshake rather than mp.Event so the
    test is portable across spawn/fork start methods and the child
    doesn't need access to the parent's interpreter state.
    """
    target = tmp_path / "note.md"
    acquired = tmp_path / "child-acquired"
    release = tmp_path / "child-release"

    ctx = mp.get_context("spawn")
    proc = ctx.Process(
        target=_hold_lock_child,
        args=(str(target), str(acquired), str(release)),
    )
    proc.start()
    try:
        # Wait for the child to acquire.
        deadline = time.monotonic() + 5.0
        while not acquired.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert acquired.is_file(), "child failed to acquire within 5s"

        # Parent acquire with a tight timeout must fail.
        with pytest.raises(VaultLockTimeout):
            with acquire(target, mode=LockMode.EXCLUSIVE, timeout=0.3):
                pass

        # Release the child; parent should now acquire cleanly.
        release.touch()
        proc.join(timeout=5.0)
        assert not proc.is_alive()
        with acquire(target, mode=LockMode.EXCLUSIVE, timeout=2.0):
            pass
    finally:
        release.touch()
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)


def test_thread_contention_resolves(tmp_path: pathlib.Path) -> None:
    """Two threads each opening their own fd serialize on flock.

    Same-process threads sharing an fd's lock state is the OS's
    default. To prove serialization we have each thread go through
    :func:`acquire`, which opens a fresh fd per call — so each
    thread holds its own fd and the OS treats them as distinct
    holders for ``LOCK_EX`` purposes.
    """
    target = tmp_path / "note.md"
    counter = {"value": 0}
    errors: list[str] = []

    def worker() -> None:
        try:
            with acquire(target, mode=LockMode.EXCLUSIVE, timeout=5.0):
                # Read-modify-write under the lock; if two threads
                # ever entered the critical section simultaneously
                # the count would race.
                snapshot = counter["value"]
                time.sleep(0.005)
                counter["value"] = snapshot + 1
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors, errors
    assert counter["value"] == 10
