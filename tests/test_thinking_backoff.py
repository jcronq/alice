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

from alice_thinking.backoff import (
    BASE_INTERVAL_SECONDS,
    MAX_INTERVAL_SECONDS,
    MIN_WAKE_PERIOD,
    apply_min_wake_period,
    detect_did_work,
    next_interval_seconds,
    read_interval,
    read_last_wake_timestamp,
    write_interval_atomic,
    write_last_wake_timestamp,
)


# ---------- next_interval_seconds: ladder + reset ----------


def test_active_mode_always_resets_to_base() -> None:
    """Active mode is never backed off — always 5 min."""
    assert (
        next_interval_seconds(
            prev_seconds=MAX_INTERVAL_SECONDS, mode="active", did_work=False
        )
        == BASE_INTERVAL_SECONDS
    )
    assert (
        next_interval_seconds(
            prev_seconds=MAX_INTERVAL_SECONDS, mode="active", did_work=True
        )
        == BASE_INTERVAL_SECONDS
    )


def test_sleep_did_work_resets_to_base() -> None:
    """Any meaningful work hard-resets the ladder."""
    for stage in ("sleep", "sleep:consolidate", "sleep:downscale", "sleep:recombine"):
        assert (
            next_interval_seconds(prev_seconds=20 * 60, mode=stage, did_work=True)
            == BASE_INTERVAL_SECONDS
        )


def test_sleep_ladder_5_10_20_30() -> None:
    """The four-step ladder: 5 → 10 → 20 → 30 minutes.

    Issue #323 lowered the cap from 40 → 30 minutes; the doubling
    step that would have produced 40 is clamped to 30 instead.
    """
    cur = BASE_INTERVAL_SECONDS
    expected = [10 * 60, 20 * 60, 30 * 60]
    for want in expected:
        cur = next_interval_seconds(
            prev_seconds=cur, mode="sleep:consolidate", did_work=False
        )
        assert cur == want


def test_sleep_ladder_caps_at_30() -> None:
    """Once at the cap, stays there on continued null passes."""
    cur = MAX_INTERVAL_SECONDS
    for _ in range(5):
        cur = next_interval_seconds(
            prev_seconds=cur, mode="sleep:consolidate", did_work=False
        )
        assert cur == MAX_INTERVAL_SECONDS


def test_max_interval_is_30_minutes() -> None:
    """Issue #323: cap must be 30 min (1800s), not 40 min (2400s).
    At 40 min the May-22 sleep cycle produced only 3 wakes / 8h
    because did_work signals from Stage B/D weren't being written."""
    assert MAX_INTERVAL_SECONDS == 1800


def test_sleep_caps_20_consecutive_idle_wakes() -> None:
    """Issue #323 regression: 20 consecutive ``did_work: false`` wakes
    must never push the interval above ``MAX_INTERVAL_SECONDS``."""
    cur = BASE_INTERVAL_SECONDS
    for _ in range(20):
        cur = next_interval_seconds(
            prev_seconds=cur, mode="sleep:consolidate", did_work=False
        )
        assert cur <= MAX_INTERVAL_SECONDS
    assert cur == MAX_INTERVAL_SECONDS


def test_sleep_below_base_floors_to_base_then_doubles() -> None:
    """A garbage prev value (e.g. 0) shouldn't degrade — floor it
    to BASE before doubling."""
    assert (
        next_interval_seconds(prev_seconds=0, mode="sleep:consolidate", did_work=False)
        == 2 * BASE_INTERVAL_SECONDS
    )
    assert (
        next_interval_seconds(
            prev_seconds=-100, mode="sleep:consolidate", did_work=False
        )
        == 2 * BASE_INTERVAL_SECONDS
    )


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
    p.write_text(f"---\nmode: sleep\nstage: B\ndid_work: {did_work}\n---\n\nbody\n")
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


# ---------- min-wake-period guard (issue #323 fix 2) ----------


def test_min_wake_period_is_30_minutes() -> None:
    """Issue #323 fix 2: cadence-level floor must be 30 min (1800s)."""
    assert MIN_WAKE_PERIOD == 1800


def test_min_period_clamps_when_recent_wake_and_high_next() -> None:
    """Last wake fired 10 min ago and next interval would be 30 min →
    clamp the next interval down to the 30 min floor. (Real case:
    backoff ran to the cap; the supervisor's elapsed-since-last is
    still inside MIN_WAKE_PERIOD; let the next wake fire on cadence.)"""
    now = 1_000_000.0
    last = now - 600  # 10 minutes ago
    clamped = apply_min_wake_period(
        2400, last_wake_ts=last, now_ts=now, min_period=1800
    )
    assert clamped == 1800


def test_min_period_no_clamp_when_no_prior_timestamp() -> None:
    """First wake after worker boot has no ``last-wake-timestamp``
    file — return the input unchanged."""
    assert apply_min_wake_period(2400, last_wake_ts=None, now_ts=1_000_000.0) == 2400


def test_min_period_no_clamp_when_last_wake_was_long_ago() -> None:
    """If elapsed >= MIN_WAKE_PERIOD the supervisor already waited at
    least the floor — leave the next interval as-is."""
    now = 1_000_000.0
    last = now - 3600  # an hour ago
    assert apply_min_wake_period(2400, last_wake_ts=last, now_ts=now) == 2400


def test_min_period_no_clamp_when_next_already_below_floor() -> None:
    """A reset-to-BASE after meaningful work must stay at BASE — never
    *raise* the interval to the floor. The guard only clamps down."""
    now = 1_000_000.0
    last = now - 60  # very recent
    assert (
        apply_min_wake_period(
            BASE_INTERVAL_SECONDS, last_wake_ts=last, now_ts=now
        )
        == BASE_INTERVAL_SECONDS
    )


def test_min_period_clamps_full_ladder_climb() -> None:
    """Composing the policy: at the top of the ladder + a recent
    last-wake, the effective next interval must equal MIN_WAKE_PERIOD."""
    cur = BASE_INTERVAL_SECONDS
    for _ in range(10):
        cur = next_interval_seconds(
            prev_seconds=cur, mode="sleep:consolidate", did_work=False
        )
    assert cur == MAX_INTERVAL_SECONDS
    now = 1_000_000.0
    last = now - 5  # essentially "just woke"
    clamped = apply_min_wake_period(cur, last_wake_ts=last, now_ts=now)
    assert clamped == MIN_WAKE_PERIOD


def test_last_wake_timestamp_roundtrip(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "last-wake-timestamp"
    write_last_wake_timestamp(p, 1_700_000_000.5)
    got = read_last_wake_timestamp(p)
    assert got is not None
    assert abs(got - 1_700_000_000.5) < 1e-3


def test_last_wake_timestamp_missing_returns_none(tmp_path: pathlib.Path) -> None:
    assert read_last_wake_timestamp(tmp_path / "absent") is None


def test_last_wake_timestamp_garbage_returns_none(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "last-wake-timestamp"
    p.write_text("not-a-float\n")
    assert read_last_wake_timestamp(p) is None


def test_last_wake_timestamp_write_is_atomic(tmp_path: pathlib.Path) -> None:
    """No ``.tmp`` leftover after a successful write."""
    p = tmp_path / "last-wake-timestamp"
    write_last_wake_timestamp(p, time.time())
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
