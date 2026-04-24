"""Backwards-compat shim — re-exports from :mod:`alice_core.events`.

The canonical location for EventLogger and the event-serialization
helpers is now alice_core. This module remains so existing imports
(``from alice_speaking.events import EventLogger, _short``) keep working
during the refactor.

The ``_short`` helper moves to :mod:`alice_core.sdk_compat` in step 4;
for now it stays here as a module-level function so nothing breaks.
"""

from __future__ import annotations

import json
from typing import Any

from alice_core.events import CapturingEmitter, EventEmitter, EventLogger


def _short(obj: Any, cap: int = 2000) -> str:
    """Truncate an arbitrary value into a short string for log fields."""
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[: cap - 1] + "…"


__all__ = ["EventEmitter", "EventLogger", "CapturingEmitter", "_short"]
