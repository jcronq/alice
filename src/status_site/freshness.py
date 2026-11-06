"""Freshness-tile classifier (agentry-dashboard "push status" tiles).

Pure server-side counterpart of ``freshnessFromTs`` in ``static/index.html``
(same 5m / 15m thresholds), extended with the dashboard's loud
"push unknown = red" rule: a tile whose source has never been seen is
``unknown`` and renders with the red treatment, not a quiet green.

States (map onto ``[data-fresh=...]`` in ``design-tokens.css``):

* ``ok``      — source updated within ``warn_after_seconds``
* ``amber``   — older than ``warn_after_seconds``
* ``red``     — older than ``bad_after_seconds``
* ``unknown`` — no source timestamp (renders red)

``render_lag_seconds`` (last_render - last_source_mtime) is reported so a
renderer that is running but reading a dead source is distinguishable from
a renderer that has stopped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEFAULT_WARN_AFTER_SECONDS = 300
DEFAULT_BAD_AFTER_SECONDS = 900

STATES: tuple[str, ...] = ("ok", "amber", "red", "unknown")


def _to_epoch(value: Any) -> float | None:
    """Accept epoch numbers, ISO strings, or datetimes; None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def classify(
    last_source_mtime: Any,
    *,
    now: Any = None,
    warn_after_seconds: float = DEFAULT_WARN_AFTER_SECONDS,
    bad_after_seconds: float = DEFAULT_BAD_AFTER_SECONDS,
) -> str:
    """Return the freshness state for a source last updated at ``last_source_mtime``.

    A future timestamp (clock skew) counts as age 0. Never raises.
    """
    src = _to_epoch(last_source_mtime)
    if src is None:
        return "unknown"
    now_s = _to_epoch(now)
    if now_s is None:
        now_s = datetime.now(timezone.utc).timestamp()
    age = max(0.0, now_s - src)
    if age >= bad_after_seconds:
        return "red"
    if age >= warn_after_seconds:
        return "amber"
    return "ok"


def tile(
    title: str,
    *,
    last_render: Any = None,
    last_source_mtime: Any = None,
    now: Any = None,
    warn_after_seconds: float = DEFAULT_WARN_AFTER_SECONDS,
    bad_after_seconds: float = DEFAULT_BAD_AFTER_SECONDS,
) -> dict[str, Any]:
    """Build the typed-dict payload a ``FreshnessTile`` widget consumes."""
    state = classify(
        last_source_mtime,
        now=now,
        warn_after_seconds=warn_after_seconds,
        bad_after_seconds=bad_after_seconds,
    )
    now_s = _to_epoch(now)
    if now_s is None:
        now_s = datetime.now(timezone.utc).timestamp()
    src = _to_epoch(last_source_mtime)
    rendered = _to_epoch(last_render)
    return {
        "title": title,
        "state": state,
        "age_seconds": None if src is None else max(0.0, now_s - src),
        "render_lag_seconds": (
            None if src is None or rendered is None else max(0.0, rendered - src)
        ),
    }
