"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.pipeline.quiet_queue_runner` (Plan 02).
"""

from .pipeline.quiet_queue_runner import *  # noqa: F401,F403
from .pipeline.quiet_queue_runner import CHECK_SECONDS, QuietQueueRunner  # noqa: F401
