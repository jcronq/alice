"""Mode selection — Plan 03 Phase 2.

``select_mode(now, vault, cfg)`` is a pure function: given local time,
a vault-state snapshot, and the resolved thinking config, return the
:class:`Mode` to drive this wake.

Phase 2 ships the function with a single hardcoded outcome
(``ActiveMode``). Phase 3 wires hour-based dispatch (active vs sleep).
Phase 4 (deferred — behavior change requires shadow-running) wires
the SleepMode sub-stage selector.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .modes import ActiveMode, Mode


def select_mode(
    *,
    now: datetime,
    vault: Any = None,
    cfg: Any = None,
) -> Mode:
    """Return the :class:`Mode` to drive this wake.

    Phase 2 contract: always returns :class:`ActiveMode` so behavior
    matches today's single-mode wake. Phase 3 swaps in hour-based
    dispatch. ``vault`` + ``cfg`` are accepted now to keep the
    signature stable across phases.
    """
    return ActiveMode()
