"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.pipeline.compaction` (Plan 02 of the
speaking refactor).

Phase 7 of plan 02 retires this shim once tests + downstream
callers have been migrated to import from the new path.
"""

from .pipeline.compaction import *  # noqa: F401,F403
from .pipeline.compaction import (  # noqa: F401
    COMPACTION_PROMPT,
    CompactionTrigger,
    DEFAULT_THRESHOLD,
    build_bootstrap_preamble,
    build_summary_preamble,
    read_summary_if_any,
    should_compact,
    write_summary,
)
