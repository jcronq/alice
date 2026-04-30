"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.pipeline.quiet_hours` (Plan 02).
"""

from .pipeline.quiet_hours import *  # noqa: F401,F403
from .pipeline.quiet_hours import (  # noqa: F401
    QueuedMessage,
    QuietQueue,
    is_quiet_hours,
)
