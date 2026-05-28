"""Container HEALTHCHECK liveness heartbeats — speaking + thinking.

The Alice container HEALTHCHECK (sandbox/Dockerfile) combines three probes:
viewer `/`, a 300s staleness window on ``/state/worker/speaking-alive``, and
a 600s window on ``/state/worker/thinking-alive``. These tests pin the
producer-side touches that keep those mtimes fresh:

- Speaking touches its liveness file on every SurfaceWatcher._run tick (the
  5-second fixed-cadence poll that fires regardless of inbound traffic).
- Thinking touches its liveness file at the start of ``wake.main()``, before
  any model calls.

Both touches go through a small ``_touch_liveness(path)`` helper so we can
monkeypatch the path constant and drive the producer against ``tmp_path``
without depending on /state/worker being writable in CI.

Background: the existing viewer-only HEALTHCHECK left a blind spot — on
2026-05-28 the container reported healthy for ~1h45m while speaking + viewer
were both hung, requiring a manual force-kill. Vault evidence: 9 similar
incidents 2026-04-26 → 2026-05-28.
"""

from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

from alice_speaking.internal import surfaces as surfaces_module
from alice_speaking.internal.surfaces import SurfaceWatcher
from alice_thinking import wake as wake_module


# ---------------------------------------------------------------------------
# Stubs for the SurfaceWatcher producer (mirrors test_idle_flush style).


class _StubStop:
    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True


class _StubCtx:
    """Minimal DaemonContext for SurfaceWatcher._run — surfaces poll
    only reads ``_stop`` and pushes to ``_queue``; nothing else is
    referenced when ``inner/surface/`` is empty."""

    def __init__(self) -> None:
        self._stop = _StubStop()
        self._queue: asyncio.Queue = asyncio.Queue()


# ---------------------------------------------------------------------------
# Speaking-side heartbeat.


def test_touch_liveness_helper_creates_file_with_current_mtime(
    tmp_path: pathlib.Path,
) -> None:
    """The bare helper is what both the unit tests below and the
    production code call — verify it does what it says (creates the
    file, mtime is now-ish)."""
    target = tmp_path / "speaking-alive"
    assert not target.exists()

    before = time.time()
    surfaces_module._touch_liveness(target)
    after = time.time()

    assert target.exists()
    mtime = target.stat().st_mtime
    assert before - 1.0 <= mtime <= after + 1.0


def test_touch_liveness_refreshes_existing_file_mtime(
    tmp_path: pathlib.Path,
) -> None:
    """A second touch updates the existing file's mtime — that's what
    the HEALTHCHECK's staleness window depends on. Verifies we're not
    accidentally short-circuiting when the file already exists."""
    target = tmp_path / "speaking-alive"
    target.touch()
    old_mtime = target.stat().st_mtime
    # Walk the mtime back so we can prove the touch moved it forward.
    import os

    past = old_mtime - 100
    os.utime(target, (past, past))
    assert target.stat().st_mtime == past

    surfaces_module._touch_liveness(target)

    new_mtime = target.stat().st_mtime
    assert new_mtime > past
    assert new_mtime >= time.time() - 1.0


def test_speaking_surface_watcher_tick_touches_liveness_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The producer loop's per-tick heartbeat is what the HEALTHCHECK
    observes. Drive one iteration of ``SurfaceWatcher._run`` against a
    ``tmp_path`` liveness override and assert the file's mtime is fresh.

    Implementation: monkeypatch ``SPEAKING_LIVENESS_PATH`` and a no-op
    ``asyncio.sleep`` so the loop body runs once and then yields. Cancel
    the task once we've observed the touch.
    """

    liveness = tmp_path / "speaking-alive"
    monkeypatch.setattr(surfaces_module, "SPEAKING_LIVENESS_PATH", liveness)

    # Surface dir under tmp_path so the producer's mkdir + glob don't
    # touch the real ~/alice-mind.
    mind_dir = tmp_path / "mind"
    mind_dir.mkdir()
    watcher = SurfaceWatcher(mind_dir)
    ctx = _StubCtx()

    async def runner() -> None:
        original_sleep = asyncio.sleep
        sleep_calls = {"n": 0}

        async def _fast_sleep(seconds: float, *args, **kwargs) -> None:
            sleep_calls["n"] += 1
            if sleep_calls["n"] == 1:
                # First poll cycle — yield zero so we can cancel.
                return None
            # Subsequent cycles — block so we can cancel cleanly.
            return await original_sleep(10.0)

        monkeypatch.setattr(surfaces_module.asyncio, "sleep", _fast_sleep)

        task = asyncio.create_task(watcher._run(ctx))
        # Yield enough for the body to run after the zero-sleep return.
        for _ in range(5):
            await original_sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(runner())

    assert liveness.exists(), (
        "SurfaceWatcher._run tick must touch the liveness file — without it "
        "the HEALTHCHECK can't tell a wedged speaking from a healthy idle one"
    )
    mtime = liveness.stat().st_mtime
    assert mtime >= time.time() - 5.0, (
        f"liveness mtime ({mtime}) is stale relative to test wallclock; "
        "the touch may have fired on the wrong path"
    )


# ---------------------------------------------------------------------------
# Thinking-side heartbeat.


def test_thinking_touch_liveness_helper_creates_file_with_current_mtime(
    tmp_path: pathlib.Path,
) -> None:
    """Same shape as the speaking helper test, on wake.py's copy. Both
    modules carry their own ``_touch_liveness`` so they can be unit-
    tested independently without cross-module patching."""
    target = tmp_path / "thinking-alive"
    assert not target.exists()

    before = time.time()
    wake_module._touch_liveness(target)
    after = time.time()

    assert target.exists()
    mtime = target.stat().st_mtime
    assert before - 1.0 <= mtime <= after + 1.0


def test_thinking_wake_main_touches_liveness_before_model_calls(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wake.main()`` must touch the liveness file BEFORE any model
    plumbing runs. We verify this by patching the path, stubbing
    ``argparse.ArgumentParser.parse_args`` to raise SystemExit (so
    ``main`` exits immediately after the touch but before
    ``_apply_config_overrides`` / ``load_model_config`` / kernel
    setup), and asserting the file was created anyway.

    If a future refactor moves the touch below argparse, this test
    catches it — the touch must fire even when ``main`` aborts on bad
    CLI args, which is the cron-tick failure case we care about.
    """
    liveness = tmp_path / "thinking-alive"
    monkeypatch.setattr(wake_module, "THINKING_LIVENESS_PATH", liveness)

    def _explode_parse_args(self):  # noqa: ANN001 — signature pinned by argparse
        raise SystemExit(2)

    monkeypatch.setattr(
        wake_module.argparse.ArgumentParser,
        "parse_args",
        _explode_parse_args,
    )

    assert not liveness.exists()

    with pytest.raises(SystemExit):
        wake_module.main()

    assert liveness.exists(), (
        "wake.main() must touch the liveness file before argparse runs — "
        "otherwise a wake that dies on a config-load exception (the exact "
        "failure mode that wedged Alice on 2026-05-28) leaves a stale mtime "
        "and the HEALTHCHECK can't tell the difference"
    )
    mtime = liveness.stat().st_mtime
    assert mtime >= time.time() - 5.0
