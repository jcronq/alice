"""Mode selection — Plan 03 Phase 3.

``select_mode(now, vault, cfg)`` is a pure function: given local
time, an optional vault-state snapshot, and the resolved thinking
config, return the :class:`Mode` to drive this wake.

Phase 3 dispatches by hour:

- 07:00–22:59 local → :class:`ActiveMode`
- 23:00–06:59 local → :class:`SleepMode` (which Phase 3 stubs to
  always pick :class:`ConsolidationStage`).

Phase 4 (deferred — behavior change) wires the SleepMode sub-stage
selector spelled out in ``inner/directive.md`` Step 0.

The hour comparison uses the tz-aware ``now.hour`` so DST
transitions Just Work — :mod:`zoneinfo` does the conversion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .modes import ActiveMode, Mode, SleepMode


# Plan 03 design: active window is 07:00–23:00 local. Outside that
# window we're in sleep. Constants kept here so Phase 3 tests + the
# Phase 1 open question (config override) have one place to look.
ACTIVE_HOUR_START = 7
ACTIVE_HOUR_END = 23


def is_active_hour(hour: int) -> bool:
    """Return True if ``hour`` falls in the active window.

    Active window is closed on the start, open on the end:
    ``[07:00, 23:00)``. So 22:xx is active; 23:00 onward is sleep.
    """
    return ACTIVE_HOUR_START <= hour < ACTIVE_HOUR_END


def select_mode(
    *,
    now: datetime,
    vault: Optional[Any] = None,
    cfg: Optional[Any] = None,
) -> Mode:
    """Return the :class:`Mode` to drive this wake.

    Phase 3 contract: hour-based dispatch. ``vault`` + ``cfg`` are
    accepted now to keep the signature stable across phases —
    Phase 4 consumes ``vault`` for SleepMode sub-stage selection.
    """
    if is_active_hour(now.hour):
        return ActiveMode()
    return SleepMode()
