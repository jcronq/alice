"""Backwards-compat shim — re-exports from :mod:`core`.

The canonical homes are:

- ``core.events`` — EventLogger + CapturingEmitter + EventEmitter.
- ``core.sdk_compat`` — _short + looks_like_missing_session.

This shim exists so existing imports (``from alice_speaking.events
import EventLogger, _short``) keep working during the refactor.
"""

from __future__ import annotations

from core.events import CapturingEmitter, EventEmitter, EventLogger
from core.sdk_compat import _short


__all__ = ["EventEmitter", "EventLogger", "CapturingEmitter", "_short"]
