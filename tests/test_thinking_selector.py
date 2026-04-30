"""Plan 03 Phase 3: selector dispatches by local hour.

Pure-function unit tests — no I/O, no time-of-day flakiness.
``select_mode`` takes an explicit ``now`` so tests pin behavior
deterministically.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from alice_thinking.modes import ActiveMode, ConsolidationStage, SleepMode
from alice_thinking.selector import is_active_hour, select_mode


WAKE_TZ = ZoneInfo("America/New_York")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 30, hour, minute, tzinfo=WAKE_TZ)


@pytest.mark.parametrize("hour", [7, 8, 12, 16, 22])
def test_selector_returns_active_during_day(hour: int) -> None:
    assert isinstance(select_mode(now=_at(hour)), ActiveMode)


@pytest.mark.parametrize("hour", [23, 0, 1, 3, 6])
def test_selector_returns_sleep_during_night(hour: int) -> None:
    assert isinstance(select_mode(now=_at(hour)), SleepMode)


def test_active_window_endpoints() -> None:
    """Boundary check: 07:00 active, 23:00 sleep, 06:59 sleep."""
    assert is_active_hour(7) is True
    assert is_active_hour(22) is True
    assert is_active_hour(23) is False
    assert is_active_hour(6) is False
    assert is_active_hour(0) is False


def test_sleep_mode_picks_consolidation_stage_phase3() -> None:
    """Phase 3 contract: SleepMode always returns ConsolidationStage.
    Phase 4 swaps in the full sub-stage selector."""
    sm = SleepMode()
    assert isinstance(sm.stage, ConsolidationStage)
    # The stage's reported name flows through to wake_start.mode
    assert sm.stage.name == "sleep:consolidate"


def test_selector_dst_aware() -> None:
    """DST: 2026-03-08 02:00 → skipped to 03:00 in America/New_York.
    Selector reads tz-aware hour, so the wake's local-hour view is
    consistent across the transition."""
    spring_forward = datetime(2026, 3, 8, 3, 30, tzinfo=WAKE_TZ)
    fall_back = datetime(2026, 11, 1, 1, 30, tzinfo=WAKE_TZ)
    # Both are <7am → sleep.
    assert isinstance(select_mode(now=spring_forward), SleepMode)
    assert isinstance(select_mode(now=fall_back), SleepMode)


def test_selector_accepts_vault_and_cfg_kwargs() -> None:
    """Phase 3 ignores them; the kwargs exist so Phase 4 can wire
    state-driven sleep sub-stage logic without changing callers."""
    mode = select_mode(now=_at(10), vault={"anything": "ignored"}, cfg={})
    assert isinstance(mode, ActiveMode)
