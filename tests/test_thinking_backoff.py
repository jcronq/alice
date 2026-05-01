"""Sleep-mode exponential backoff policy.

Pure-function tests for the ladder + reset rules, plus the
filesystem helpers (atomic interval IO and did_work detection
from wake-file frontmatter). Spec:
``cortex-memory/research/2026-05-01-sleep-mode-exponential-backoff-design.md``.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

from alice_thinking.backoff import (
    BASE_INTERVAL_SECONDS,
    MAX_INTERVAL_SECONDS,
    detect_did_work,
    next_interval_seconds,
    read_interval,
    write_interval_atomic,
)


# ---------- next_interval_seconds: ladder + reset ----------


def test_active_mode_always_resets_to_base() -> None:
    """Active mode is never backed off — always 5 min."""
    assert next_interval_seconds(
        prev_seconds=MAX_INTERVAL_SECONDS, mode="active", did_work=False
    ) == BASE_INTERVAL_SECONDS
    assert next_interval_seconds(
        prev_seconds=MAX_INTERVAL_SECONDS, mode="active", did_work=True
    ) == BASE_INTERVAL_SECONDS


def test_sleep_did_work_resets_to_base() -> None:
    """Any meaningful work hard-resets the ladder."""
    for stage in ("sleep", "sleep:consolidate", "sleep:downscale", "sleep:recombine"):
        assert next_interval_seconds(
            prev_seconds=20 * 60, mode=stage, did_work=True
        ) == BASE_INTERVAL_SECONDS


def test_sleep_ladder_5_10_20_40() -> None:
    """The four-step ladder: 5 → 10 → 20 → 40 minutes."""
    cur = BASE_INTERVAL_SECONDS
    expected = [10 * 60, 20 * 60, 40 * 60]
    for want in expected:
        cur = next_interval_seconds(
            prev_seconds=cur, mode="sleep:consolidate", did_work=False
        )
        assert cur == want


def test_sleep_ladder_caps_at_40() -> None:
    """Once at 40 min, stays at 40 min on continued null passes."""
    cur = MAX_INTERVAL_SECONDS
    for _ in range(5):
        cur = next_interval_seconds(
            prev_seconds=cur, mode="sleep:consolidate", did_work=False
        )
        assert cur == MAX_INTERVAL_SECONDS


def test_sleep_below_base_floors_to_base_then_doubles() -> None:
    """A garbage prev value (e.g. 0) shouldn't degrade — floor it
    to BASE before doubling."""
    assert next_interval_seconds(
        prev_seconds=0, mode="sleep:consolidate", did_work=False
    ) == 2 * BASE_INTERVAL_SECONDS
    assert next_interval_seconds(
        prev_seconds=-100, mode="sleep:consolidate", did_work=False
    ) == 2 * BASE_INTERVAL_SECONDS


# ---------- atomic interval file IO ----------


def test_write_and_read_roundtrip(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "next-thinking-interval-seconds"
    write_interval_atomic(p, 1200)
    assert read_interval(p) == 1200


def test_write_creates_parent_dir(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "deeper" / "interval"
    write_interval_atomic(p, 600)
    assert p.is_file()
    assert read_interval(p) == 600


def test_read_clamps_below_base(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "interval"
    p.write_text("30\n")  # well below 5 min
    assert read_interval(p) == BASE_INTERVAL_SECONDS


def test_read_clamps_above_max(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "interval"
    p.write_text("99999\n")
    assert read_interval(p) == MAX_INTERVAL_SECONDS


def test_read_missing_file_returns_default(tmp_path: pathlib.Path) -> None:
    assert read_interval(tmp_path / "missing") == BASE_INTERVAL_SECONDS


def test_read_garbage_returns_default(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "interval"
    p.write_text("not-an-int\n")
    assert read_interval(p) == BASE_INTERVAL_SECONDS


def test_write_is_atomic_no_tmp_leftover(tmp_path: pathlib.Path) -> None:
    """tmp file must be replaced, not lingering as `.tmp`."""
    p = tmp_path / "interval"
    write_interval_atomic(p, 600)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


# ---------- did_work detection ----------


def _write_wake(
    mind: pathlib.Path,
    *,
    day: str,
    hhmmss: str,
    did_work: str,
    mtime: float | None = None,
) -> pathlib.Path:
    d = mind / "inner" / "thoughts" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{hhmmss}-wake.md"
    p.write_text(
        "---\n"
        "mode: sleep\n"
        "stage: B\n"
        f"did_work: {did_work}\n"
        "---\n\n"
        "body\n"
    )
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_did_work_true_when_recent_wake_says_so(tmp_path: pathlib.Path) -> None:
    since = time.time() - 10
    _write_wake(tmp_path, day="2026-05-01", hhmmss="010203", did_work="true")
    assert detect_did_work(tmp_path, since_ts=since) is True


def test_did_work_false_when_recent_wake_did_no_work(tmp_path: pathlib.Path) -> None:
    since = time.time() - 10
    _write_wake(tmp_path, day="2026-05-01", hhmmss="010203", did_work="false")
    assert detect_did_work(tmp_path, since_ts=since) is False


def test_did_work_ignores_files_older_than_since(tmp_path: pathlib.Path) -> None:
    """A truthy wake from before since_ts shouldn't count — that
    was a previous wake's signal, not this one's."""
    since = time.time()
    p = _write_wake(
        tmp_path,
        day="2026-05-01",
        hhmmss="010203",
        did_work="true",
        mtime=since - 600,
    )
    assert p.stat().st_mtime < since
    assert detect_did_work(tmp_path, since_ts=since) is False


def test_did_work_default_false_on_missing_dir(tmp_path: pathlib.Path) -> None:
    """No thoughts dir at all = idle wake = back off."""
    assert detect_did_work(tmp_path, since_ts=0) is False


def test_did_work_picks_up_truthy_among_mixed(tmp_path: pathlib.Path) -> None:
    """Multiple wake files in the window: any did_work=true wins."""
    since = time.time() - 10
    _write_wake(tmp_path, day="2026-05-01", hhmmss="000001", did_work="false")
    _write_wake(tmp_path, day="2026-05-01", hhmmss="000002", did_work="true")
    assert detect_did_work(tmp_path, since_ts=since) is True
