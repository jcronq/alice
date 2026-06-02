"""Phase 5 (2026-06-02) cutover: selector always returns ActiveMode.

Pure-function unit tests — no I/O, no time-of-day flakiness.
``select_mode`` takes an explicit ``now`` so tests pin behavior
deterministically.

Pre-phase-5, the selector dispatched between :class:`ActiveMode` and
:class:`SleepMode` on local hour. Phase 5 retired ``SleepMode`` (the
former sleep-stage work moved to the ``alice-memory-worker`` service);
the selector now returns :class:`ActiveMode` unconditionally.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from alice_thinking.modes import ActiveMode
from alice_thinking.selector import is_active_hour, select_mode


WAKE_TZ = ZoneInfo("America/New_York")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 30, hour, minute, tzinfo=WAKE_TZ)


@pytest.mark.parametrize("hour", [7, 8, 12, 16, 22])
def test_selector_returns_active_during_day(hour: int) -> None:
    assert isinstance(select_mode(now=_at(hour)), ActiveMode)


@pytest.mark.parametrize("hour", [23, 0, 1, 3, 6])
def test_selector_returns_active_at_night_post_phase5(hour: int) -> None:
    """Phase 5: night-hour wakes route to ActiveMode (sleep retired)."""
    assert isinstance(select_mode(now=_at(hour)), ActiveMode)


def test_active_window_endpoints() -> None:
    """Post phase 5, every hour is active — the function exists for
    back-compat with external callers that key off the schedule."""
    for h in (0, 6, 7, 22, 23):
        assert is_active_hour(h) is True


def test_selector_dst_aware() -> None:
    """DST transitions don't change dispatch — thinking is single-mode."""
    spring_forward = datetime(2026, 3, 8, 3, 30, tzinfo=WAKE_TZ)
    fall_back = datetime(2026, 11, 1, 1, 30, tzinfo=WAKE_TZ)
    assert isinstance(select_mode(now=spring_forward), ActiveMode)
    assert isinstance(select_mode(now=fall_back), ActiveMode)


def test_selector_accepts_vault_and_cfg_kwargs() -> None:
    """Phase 5 ignores them; the kwargs exist so back-compat callers
    that still pass ``vault`` / ``cfg`` don't break."""
    mode = select_mode(now=_at(10), vault={"anything": "ignored"}, cfg={})
    assert isinstance(mode, ActiveMode)
