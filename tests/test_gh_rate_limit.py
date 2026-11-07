"""Tests for ``alice_forge.rate_limit`` — M3 pre-flight + M4 backoff.

Covers:
  * :func:`check_rate_limit` payload parsing (happy path, failures)
  * :class:`BackoffState` round-trip + atomicity
  * :func:`record_rate_limit_hit` exponential schedule
  * :func:`should_defer` decision table (fresh / stale / low / backoff)
  * :func:`observe_gh_error` records only for rate-limit-shaped errors
  * :func:`estimate_worker_cost` table lookup

Issue #786 M3 + M4.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from alice_forge import rate_limit as rl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    """Return a stand-in for :class:`subprocess.CompletedProcess`."""
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")


def _fake_now(utc: dt.datetime):
    """Build a frozen-clock callable returning ``utc``."""

    def _now() -> dt.datetime:
        return utc

    return _now


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------


def test_check_rate_limit_parses_core_and_graphql() -> None:
    payload = {
        "core": {"limit": 5000, "remaining": 3448, "reset": 1_800_000_000, "used": 1552},
        "graphql": {"limit": 5000, "remaining": 4010, "reset": 1_800_003_600, "used": 990},
        "search": {"limit": 30, "remaining": 29, "reset": 1_800_000_060, "used": 1},
    }

    def run(*_a, **_kw):
        return _fake_completed(stdout=json.dumps(payload))

    status = rl.check_rate_limit(run=run)
    assert status is not None
    assert status.core_remaining == 3448
    assert status.graphql_remaining == 4010
    # Reset times rendered as ISO 8601 UTC.
    assert status.core_reset_at.endswith("Z")
    assert status.raw == payload


def test_check_rate_limit_handles_gh_failure() -> None:
    def run(*_a, **_kw):
        return _fake_completed(stdout="", returncode=1)

    assert rl.check_rate_limit(run=run) is None


def test_check_rate_limit_handles_timeout() -> None:
    def run(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=10)

    assert rl.check_rate_limit(run=run) is None


def test_check_rate_limit_handles_malformed_json() -> None:
    def run(*_a, **_kw):
        return _fake_completed(stdout="not json")

    assert rl.check_rate_limit(run=run) is None


# ---------------------------------------------------------------------------
# BackoffState file round-trip
# ---------------------------------------------------------------------------


def test_backoff_state_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    state = rl.read_backoff_state(tmp_path / "missing.json")
    assert state.core_remaining is None
    assert state.backoff_until is None
    assert state.consecutive_hits == 0


def test_backoff_state_malformed_json_returns_empty(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json at all")
    state = rl.read_backoff_state(path)
    assert state.backoff_until is None


def test_backoff_state_round_trip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    original = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4200,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4800,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until="2026-11-07T00:01:00Z",
        backoff_reason="secondary rate limit observed",
        consecutive_hits=2,
    )
    rl.write_backoff_state(original, path)
    reloaded = rl.read_backoff_state(path)
    assert reloaded == original


def test_write_backoff_state_is_atomic(tmp_path: pathlib.Path) -> None:
    """Rename-replace means a reader never observes a torn payload."""
    path = tmp_path / "backoff.json"

    # Pre-existing valid payload the reader should see uninterrupted.
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=1,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=1,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    rl.write_backoff_state(seed, path)

    # Write a new payload — behind the scenes this is temp + rename.
    new = rl.BackoffState(
        last_updated="2026-11-07T00:05:00Z",
        core_remaining=9999,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=9999,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    rl.write_backoff_state(new, path)

    # Reader sees the full new payload (never a half-written file).
    reloaded = rl.read_backoff_state(path)
    assert reloaded.core_remaining == 9999
    assert reloaded.last_updated == "2026-11-07T00:05:00Z"

    # No orphaned tmp files left behind.
    assert not list(tmp_path.glob("*.tmp.*"))


# ---------------------------------------------------------------------------
# record_rate_limit_hit — exponential schedule
# ---------------------------------------------------------------------------


def test_record_rate_limit_hit_starts_at_initial_delay(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)

    # jitter=0 to make the schedule deterministic.
    state = rl.record_rate_limit_hit(
        reason="secondary rate limit",
        path=path,
        now=_fake_now(frozen),
        jitter=lambda lo, hi: 0.0,
    )
    assert state.consecutive_hits == 1
    assert state.backoff_reason == "secondary rate limit"

    backoff_dt = dt.datetime.fromisoformat(state.backoff_until.replace("Z", "+00:00"))
    delta = (backoff_dt - frozen).total_seconds()
    # Initial = 60s with jitter=0.
    assert delta == pytest.approx(60.0, abs=0.5)


def test_record_rate_limit_hit_doubles_each_time(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)

    for expected_hits, expected_delay in [(1, 60), (2, 120), (3, 240), (4, 480)]:
        state = rl.record_rate_limit_hit(
            reason="x",
            path=path,
            now=_fake_now(frozen),
            jitter=lambda lo, hi: 0.0,
        )
        assert state.consecutive_hits == expected_hits
        backoff_dt = dt.datetime.fromisoformat(
            state.backoff_until.replace("Z", "+00:00")
        )
        delta = (backoff_dt - frozen).total_seconds()
        assert delta == pytest.approx(expected_delay, abs=0.5)


def test_record_rate_limit_hit_caps_at_ceiling(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)

    # Pre-seed to a hit count that would overflow past the 900s ceiling.
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=None,
        core_reset_at=None,
        graphql_remaining=None,
        graphql_reset_at=None,
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=10,  # 2^10 * 60 = 61440s, well past ceiling
    )
    rl.write_backoff_state(seed, path)

    state = rl.record_rate_limit_hit(
        reason="x",
        path=path,
        now=_fake_now(frozen),
        jitter=lambda lo, hi: 0.0,
    )
    backoff_dt = dt.datetime.fromisoformat(state.backoff_until.replace("Z", "+00:00"))
    delta = (backoff_dt - frozen).total_seconds()
    assert delta == pytest.approx(900.0, abs=0.5)


def test_clear_backoff_resets_window_and_hits(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until="2026-11-07T00:10:00Z",
        backoff_reason="x",
        consecutive_hits=3,
    )
    rl.write_backoff_state(seed, path)
    cleared = rl.clear_backoff(path)
    assert cleared.backoff_until is None
    assert cleared.consecutive_hits == 0
    # Non-backoff fields preserved.
    assert cleared.core_remaining == 4000


# ---------------------------------------------------------------------------
# should_defer decision table
# ---------------------------------------------------------------------------


def test_should_defer_no_cached_state_fails_open(tmp_path: pathlib.Path) -> None:
    """A missing / brand-new backoff file must not deadlock callers."""
    decision = rl.should_defer(path=tmp_path / "nonexistent.json")
    assert decision.defer is False


def test_should_defer_active_backoff_defers(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until="2026-11-07T00:02:00Z",
        backoff_reason="secondary rate limit",
        consecutive_hits=1,
    )
    rl.write_backoff_state(seed, path)

    decision = rl.should_defer(path=path, now=_fake_now(frozen))
    assert decision.defer is True
    assert "backoff active" in decision.reason
    assert decision.retry_after_s == pytest.approx(120.0, abs=1.0)


def test_should_defer_expired_backoff_proceeds(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 10, 0, tzinfo=dt.timezone.utc)
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until="2026-11-07T00:02:00Z",  # in the past now
        backoff_reason="x",
        consecutive_hits=1,
    )
    rl.write_backoff_state(seed, path)
    decision = rl.should_defer(path=path, now=_fake_now(frozen))
    assert decision.defer is False


def test_should_defer_core_below_floor(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=100,  # below default 500
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    rl.write_backoff_state(seed, path)
    decision = rl.should_defer(path=path, now=_fake_now(frozen))
    assert decision.defer is True
    assert "core.remaining=100" in decision.reason


def test_should_defer_graphql_below_floor(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=10,  # below default 500
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    rl.write_backoff_state(seed, path)
    decision = rl.should_defer(path=path, now=_fake_now(frozen))
    assert decision.defer is True
    assert "graphql.remaining=10" in decision.reason


def test_should_defer_within_budget_proceeds(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=3000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=3000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    rl.write_backoff_state(seed, path)
    decision = rl.should_defer(path=path, now=_fake_now(frozen))
    assert decision.defer is False
    assert decision.retry_after_s is None


def test_should_defer_fresh_status_short_circuits_staleness(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "missing.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)
    status = rl.RateLimitStatus(
        core_remaining=100,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
    )
    decision = rl.should_defer(path=path, now=_fake_now(frozen), status=status)
    assert decision.defer is True
    assert "core.remaining=100" in decision.reason


def test_should_defer_custom_floors_respected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    frozen = dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc)
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=1000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=1000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    rl.write_backoff_state(seed, path)
    # Raise floor above the cached remaining so the gate fires.
    decision = rl.should_defer(
        path=path, min_core=2000, min_graphql=2000, now=_fake_now(frozen)
    )
    assert decision.defer is True


# ---------------------------------------------------------------------------
# observe_gh_error
# ---------------------------------------------------------------------------


class _FakeGhErr:
    """Duck-typed stand-in for the dispatcher's GHCommandError."""

    def __init__(self, is_rate_limit: bool, msg: str = "") -> None:
        self.looks_like_rate_limit = is_rate_limit
        self._msg = msg

    def __str__(self) -> str:
        return self._msg


def test_observe_gh_error_records_rate_limit(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    err = _FakeGhErr(is_rate_limit=True, msg="secondary rate limit exceeded")
    result = rl.observe_gh_error(
        err, path=path, jitter=lambda lo, hi: 0.0
    )
    assert result is not None
    assert result.consecutive_hits == 1
    assert result.backoff_until is not None
    assert "secondary rate limit" in result.backoff_reason


def test_observe_gh_error_ignores_non_rate_limit(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    err = _FakeGhErr(is_rate_limit=False, msg="404 not found")
    assert rl.observe_gh_error(err, path=path) is None
    # File not created for non-RL errors.
    assert not path.exists()


# ---------------------------------------------------------------------------
# update_rate_limit_snapshot + wait_until_available
# ---------------------------------------------------------------------------


def test_update_snapshot_preserves_backoff_window(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "backoff.json"
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until="2026-11-07T00:05:00Z",
        backoff_reason="secondary rate limit",
        consecutive_hits=2,
    )
    rl.write_backoff_state(seed, path)
    status = rl.RateLimitStatus(
        core_remaining=4500,
        core_reset_at="2026-11-07T02:00:00Z",
        graphql_remaining=4500,
        graphql_reset_at="2026-11-07T02:00:00Z",
    )
    merged = rl.update_rate_limit_snapshot(status, path)
    # Snapshot updated.
    assert merged.core_remaining == 4500
    # Backoff window preserved.
    assert merged.backoff_until == "2026-11-07T00:05:00Z"
    # But consecutive_hits reset to 0 — a clean snapshot means the prior
    # hit's window has elapsed.
    assert merged.consecutive_hits == 0


def test_wait_until_available_returns_true_when_clean(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "backoff.json"

    payload = {
        "core": {"remaining": 4000, "reset": 1_800_000_000, "used": 0, "limit": 5000},
        "graphql": {"remaining": 4000, "reset": 1_800_000_000, "used": 0, "limit": 5000},
    }

    def run(*_a, **_kw):
        return _fake_completed(stdout=json.dumps(payload))

    called: list[float] = []
    ok = rl.wait_until_available(
        path=path,
        timeout_s=30.0,
        sleep=lambda s: called.append(s),
        run=run,
    )
    assert ok is True
    # No sleep needed on first pass.
    assert called == []


def test_wait_until_available_times_out_on_persistent_backoff(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "backoff.json"
    # Seed an active backoff window far in the future.
    seed = rl.BackoffState(
        last_updated="2026-11-07T00:00:00Z",
        core_remaining=4000,
        core_reset_at="2026-11-07T01:00:00Z",
        graphql_remaining=4000,
        graphql_reset_at="2026-11-07T01:00:00Z",
        backoff_until="2099-01-01T00:00:00Z",
        backoff_reason="x",
        consecutive_hits=1,
    )
    rl.write_backoff_state(seed, path)

    # Frozen clock that advances past the deadline on each call.
    times = iter(
        [
            dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 7, 0, 0, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 7, 1, 0, 0, tzinfo=dt.timezone.utc),  # past deadline
        ]
    )

    def now_fn() -> dt.datetime:
        return next(times)

    # check_rate_limit returns None → snapshot not refreshed.
    def run(*_a, **_kw):
        return _fake_completed(stdout="", returncode=1)

    ok = rl.wait_until_available(
        path=path,
        timeout_s=1.0,
        sleep=lambda s: None,
        run=run,
        now=now_fn,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# estimate_worker_cost
# ---------------------------------------------------------------------------


def test_estimate_worker_cost_known_kinds() -> None:
    assert rl.estimate_worker_cost("sm-code-worker") == {"core": 20, "graphql": 5}
    assert rl.estimate_worker_cost("sm-thinking-agent") == {"core": 10, "graphql": 10}


def test_estimate_worker_cost_unknown_falls_back() -> None:
    # Unknown falls back to the conservative "unknown" row.
    assert rl.estimate_worker_cost("mystery") == rl.estimate_worker_cost("unknown")
