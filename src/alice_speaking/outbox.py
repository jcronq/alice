"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.pipeline.outbox` (Plan 02).
"""

from .pipeline.outbox import *  # noqa: F401,F403
from .pipeline.outbox import OutboxRouter, TransportLookup  # noqa: F401
