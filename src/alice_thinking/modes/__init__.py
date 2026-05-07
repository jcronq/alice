"""Wake-time modes for the thinking hemisphere.

The :class:`Mode` protocol survives as the runtime abstraction;
``ActiveMode`` and ``SleepMode`` now wrap :class:`PhaseRunner`
(see ``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``).
``ConsolidationStage`` was retired — its behavior is subsumed by the
``sleep-b.md`` phase fragment.
"""

from .active import ActiveMode
from .base import Mode, WakeContext
from .sleep import SleepMode


__all__ = [
    "ActiveMode",
    "Mode",
    "SleepMode",
    "WakeContext",
]
