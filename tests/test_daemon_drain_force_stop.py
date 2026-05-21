"""Tests for the speaking daemon's shutdown bounding (2026-05-21 hang fix).

The production incident: a SIGTERM put the daemon into drain mode, the
drain attempted to cancel an in-flight subagent task whose underlying
claude CLI subprocess didn't honor CancelledError, and the awaiting
``contextlib.suppress(BaseException): await task`` blocked forever. A
second SIGTERM logged "force-stopping" but its only effect was setting
``self._stop`` -- no one was waiting on it anymore -- and the parent
hung in S/ep_poll for 30 minutes until ``docker kill`` arrived.

The fix has three layers, ordered cheapest-to-most-aggressive:

1. ``asyncio.wait(tasks, timeout=N)`` -- genuinely bounded join. Returns
   ``(done, pending)`` without re-awaiting pending tasks. Subtle but
   critical: ``asyncio.wait_for(asyncio.gather(...), timeout=N)`` is
   NOT bounded -- when a child swallows ``CancelledError``, wait_for's
   internal ``_cancel_and_wait`` hangs waiting for the gather to die.
2. ``_stop_with_timeout`` wraps each transport's ``.stop()`` call so a
   single sync-blocking ``stop()`` can't wedge the whole shutdown.
3. ``threading.Timer`` armed on the SECOND signal -- daemon thread
   that calls ``os._exit(1)`` if the event loop is wedged past the
   force-stop budget. This is the absolute backstop and the only
   guarantee that survives a fully-uncancellable task.

These tests verify layer 1 (the asyncio primitive choice). The
wall-clock backstop is deliberately not exercised here -- a test that
calls ``os._exit`` would be hostile to the test runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest


# ---------------------------------------------------------------------
# Bounded-cancel primitive: asyncio.wait(timeout=N).
# ---------------------------------------------------------------------


def _make_swallow_cancel_task(stop: asyncio.Event) -> asyncio.Task:
    """Build a task that swallows CancelledError until ``stop`` is set.

    Mirrors the production bug class (a subagent whose underlying claude
    CLI subprocess doesn't honor CancelledError) but stays test-safe:
    setting ``stop`` at teardown lets the task exit cleanly so we don't
    leak a runaway coroutine into pytest-asyncio's loop close.
    """

    async def _runner() -> None:
        while not stop.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                # The bug class: catch and continue.
                continue

    return asyncio.create_task(_runner(), name="bad")


async def _well_behaved() -> None:
    await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_asyncio_wait_returns_within_budget_when_task_swallows_cancel() -> None:
    """``asyncio.wait(timeout=N)`` MUST return within N seconds even when
    a task swallows CancelledError. This is the root-cause guard for the
    2026-05-21 incident.
    """
    stop = asyncio.Event()
    bad = _make_swallow_cancel_task(stop)
    good = asyncio.create_task(_well_behaved(), name="good")
    tasks = [bad, good]

    await asyncio.sleep(0.05)  # let tasks enter their sleep
    for t in tasks:
        t.cancel()

    start = time.monotonic()
    done, pending = await asyncio.wait(tasks, timeout=0.5)
    elapsed = time.monotonic() - start

    # Budget honored: even though ``bad`` swallows CancelledError, the
    # caller returned within the timeout (with CI slack).
    assert elapsed < 2.0, f"asyncio.wait exceeded budget: {elapsed:.2f}s"

    # ``good`` honored cancel and shows up in ``done``.
    assert good in done
    # ``bad`` ignored cancel and shows up in ``pending``. The daemon
    # logs these as stragglers and moves on; the wall-clock os._exit
    # guard is the absolute backstop if anything still wedges.
    assert bad in pending

    # Teardown: release the swallow loop so loop close is clean.
    stop.set()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(bad, timeout=1.0)


@pytest.mark.asyncio
async def test_asyncio_wait_finishes_promptly_when_tasks_well_behaved() -> None:
    """Happy path must not add latency."""
    tasks = [
        asyncio.create_task(_well_behaved(), name=f"t{i}") for i in range(3)
    ]
    await asyncio.sleep(0.05)
    for t in tasks:
        t.cancel()

    start = time.monotonic()
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"happy path too slow: {elapsed:.2f}s"
    assert all(t.done() for t in tasks)
    assert not pending


@pytest.mark.asyncio
async def test_wait_for_gather_is_NOT_bounded_when_cancel_is_swallowed() -> None:
    """Document the trap that the production fix avoids.

    ``asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
    timeout=N)`` is the obvious-looking but WRONG choice: when any task
    swallows ``CancelledError``, gather can't complete, and wait_for's
    internal ``_cancel_and_wait`` hangs waiting for the gather to die.

    We assert this by running the broken pattern with a TIGHTER outer
    timeout than the inner budget -- if the outer fires first, the
    inner was indeed unbounded. This locks in the rationale; if a future
    Python release makes wait_for genuinely bounded on this case, the
    test will fail loudly and we can simplify.
    """
    stop = asyncio.Event()
    bad = _make_swallow_cancel_task(stop)
    await asyncio.sleep(0.05)
    bad.cancel()

    async def _broken_pattern() -> None:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(
                asyncio.gather(bad, return_exceptions=True),
                timeout=0.3,
            )

    # Bound the outer wait with ``asyncio.wait`` (the genuinely-bounded
    # primitive) so the test itself can't fall into the same trap.
    # ``asyncio.wait_for`` for the outer would re-hang for the same
    # reason _broken_pattern is hanging.
    broken_task = asyncio.create_task(_broken_pattern(), name="broken")
    start = time.monotonic()
    done, pending = await asyncio.wait({broken_task}, timeout=1.5)
    elapsed = time.monotonic() - start

    assert broken_task in pending and elapsed >= 1.4, (
        "wait_for(gather(...)) returned within budget — "
        "the production fix's premise may no longer hold; "
        "review whether asyncio.wait is still required."
    )

    # Teardown.
    stop.set()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(bad, timeout=1.0)


# ---------------------------------------------------------------------
# Multiprocessing-child terminate->join(timeout)->kill ordering. The
# daemon doesn't currently hold direct mp.Process references (the SDK
# manages its own subprocess), but this codifies the contract for any
# future child reference.
# ---------------------------------------------------------------------


class _FakeMpChild:
    def __init__(self, *, honors_terminate: bool) -> None:
        self.calls: list[str] = []
        self._honors_terminate = honors_terminate
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self._honors_terminate:
            self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.calls.append(f"join(timeout={timeout})")

    def kill(self) -> None:
        self.calls.append("kill")
        self._alive = False


def _force_stop_mp_child(child: _FakeMpChild, *, join_timeout: float) -> None:
    """Template the daemon's force-stop branch must follow if/when it
    grows a direct mp.Process reference."""
    child.terminate()
    child.join(timeout=join_timeout)
    if child.is_alive():
        child.kill()


def test_mp_child_terminate_join_kill_ordering_when_terminate_honored() -> None:
    child = _FakeMpChild(honors_terminate=True)
    _force_stop_mp_child(child, join_timeout=2.0)
    assert child.calls == ["terminate", "join(timeout=2.0)"]
    assert not child.is_alive()


def test_mp_child_falls_through_to_kill_when_terminate_ignored() -> None:
    child = _FakeMpChild(honors_terminate=False)
    _force_stop_mp_child(child, join_timeout=2.0)
    assert child.calls == ["terminate", "join(timeout=2.0)", "kill"]
    assert not child.is_alive()
