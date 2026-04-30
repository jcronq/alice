"""Wake-time modes for the thinking hemisphere.

Plan 03 Phase 2 introduces the :class:`Mode` protocol; the selector
picks a Mode based on local time + vault state and the wake calls
its ``build_prompt`` + ``post_run`` around the kernel call.
"""

from .active import ActiveMode
from .base import Mode, WakeContext


__all__ = ["ActiveMode", "Mode", "WakeContext"]
