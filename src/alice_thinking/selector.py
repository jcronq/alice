"""Mode selection — Plan 03 Phase 3.

``select_mode(now, vault, cfg)`` is a pure function: given local
time, an optional vault-state snapshot, and the resolved thinking
config, return the :class:`Mode` to drive this wake.

Phase 5 of the memory-worker extraction (2026-06-02) retired
:class:`SleepMode` — thinking is single-mode and always returns
:class:`ActiveMode`. The hour-based dispatch the original
implementation did is no longer needed because the former sleep-stage
work (B/C/D) moved to the ``alice-memory-worker`` service. The
function signature is preserved for callers that still pass ``now``
+ ``vault``; both are accepted and ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .modes import ActiveMode, Mode


# Plan 03 design: active window was 07:00–23:00 local. Phase 5 (2026-06-02)
# made it 24/7 — thinking is single-mode. Constants kept for any external
# caller that reads ``is_active_hour`` for its own bookkeeping.
ACTIVE_HOUR_START = 0
ACTIVE_HOUR_END = 24


def is_active_hour(hour: int) -> bool:
    """Return True for any hour — thinking is always active post phase 5.

    Kept as a pure function so external callers reading the schedule
    don't break. Use :func:`select_mode` for the live dispatch.
    """
    return ACTIVE_HOUR_START <= hour < ACTIVE_HOUR_END


def select_mode(
    *,
    now: datetime,
    vault: Optional[Any] = None,
    cfg: Optional[Any] = None,
) -> Mode:
    """Return the :class:`Mode` to drive this wake.

    Phase 5 contract: always :class:`ActiveMode`. ``now``, ``vault``,
    ``cfg`` are accepted for back-compat but unused — the dispatch is
    unconditional.
    """
    del now, vault, cfg  # accepted for back-compat; unused.
    return ActiveMode()
