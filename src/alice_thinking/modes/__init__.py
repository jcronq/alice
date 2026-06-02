"""Wake-time modes for the thinking hemisphere.

The :class:`Mode` protocol survives as the runtime abstraction. Phase 5
of the memory-worker extraction (2026-06-02) retired ``SleepMode`` —
thinking is single-mode (always generative) and the former sleep-stage
work (B/C/D) moved to the ``alice-memory-worker`` service.
"""

from .active import ActiveMode
from .base import Mode, WakeContext


__all__ = [
    "ActiveMode",
    "Mode",
    "WakeContext",
]
