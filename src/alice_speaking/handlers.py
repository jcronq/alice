"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.pipeline.handlers` (Plan 02).
"""

from .pipeline.handlers import *  # noqa: F401,F403
from .pipeline.handlers import (  # noqa: F401
    CLITraceHandler,
    CompactionArmer,
    SessionHandler,
)
