"""Small helpers that paper over Claude Agent SDK quirks.

Two things live here:

- :func:`_short` — truncate arbitrary values for log fields. The SDK hands
  back strings, dicts, bytes, dataclass instances — anything JSON-
  serializable. This helper flattens it to a capped string so log lines
  never blow up the observability stream.
- :func:`looks_like_missing_session` — the SDK raises different exception
  classes across minor versions when ``resume=<stale_id>`` points at a
  session whose on-disk JSONL no longer exists. Rather than importing a
  specific class (fragile across versions), we pattern-match on the
  exception name + message. Daemon callers use this to decide whether to
  drop the session and retry with a fresh one.
"""

from __future__ import annotations

import json
from typing import Any


# Exception class names we've observed the SDK emit when resume= fails
# because the session JSONL no longer exists on disk. Loose match so we
# don't couple to a specific SDK version's class layout.
_SESSION_MISSING_EXC_NAMES = {
    "SessionNotFoundError",
    "NoSuchSessionError",
    "SessionNotFound",
}


def _short(obj: Any, cap: int = 2000) -> str:
    """Truncate an arbitrary value into a short string for log fields.

    Non-strings are dumped with ``json.dumps(..., default=str)`` so the
    logger never crashes on unknown objects. Caller picks the cap — the
    daemon uses 2000 (generous, fits most block contents); thinking uses
    400 (tight, keeps the log compact for mostly-internal traces).
    """
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[: cap - 1] + "…"


def looks_like_missing_session(exc: BaseException) -> bool:
    """True if ``exc`` looks like the SDK complaining about a stale
    ``resume=`` pointer.

    Used by the speaking daemon's retry-in-place path — the daemon drops
    its session_id, primes the turn_log bootstrap preamble, and retries
    the same prompt with a fresh session.
    """
    name = type(exc).__name__
    if name in _SESSION_MISSING_EXC_NAMES:
        return True
    msg = str(exc).lower()
    return (
        "session" in msg
        and ("not found" in msg or "no such" in msg or "does not exist" in msg)
    )


__all__ = ["_short", "looks_like_missing_session"]
