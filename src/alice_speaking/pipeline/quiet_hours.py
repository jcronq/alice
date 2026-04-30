"""Quiet hours policy + queued-outbound persistence.

During quiet hours the daemon still processes turns and runs surface
reviews — Alice is present but silent. Outbound on durable transports
(signal, discord) is held until the window closes, then drained
in-order. CLI bypasses (interactive — the user is at a terminal waiting).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo


@dataclass
class QueuedMessage:
    """A queued outbound. ``transport`` is required to know which
    transport to dispatch on at drain time. Defaults to ``"signal"`` for
    back-compat with pre-Phase-4 on-disk records (signal-only)."""

    recipient: str
    text: str
    queued_at: float
    transport: str = "signal"


def is_quiet_hours(cfg_speaking: dict[str, Any], now: dt.datetime | None = None) -> bool:
    """True if we are currently in quiet hours per the speaking config.

    Accepts a speaking config dict like ``cfg.speaking`` directly so the
    function is trivial to unit-test.
    """
    qh = (cfg_speaking or {}).get("quiet_hours") or {}
    if not qh:
        return False
    tz_name = qh.get("timezone", "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — bad tz → never-quiet rather than crash
        return False
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz).time()
    try:
        start = dt.time.fromisoformat(qh.get("start", "22:00"))
        end = dt.time.fromisoformat(qh.get("end", "07:00"))
    except ValueError:
        return False
    if start <= end:
        return start <= current < end
    # Wraps midnight — e.g., 22:00 → 07:00.
    return current >= start or current < end


class QuietQueue:
    """Append-only JSONL queue of messages held during quiet hours."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def append(self, msg: QueuedMessage) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

    def drain(self) -> list[QueuedMessage]:
        if not self.path.is_file():
            return []
        out: list[QueuedMessage] = []
        for raw in self.path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            out.append(QueuedMessage(**obj))
        # Atomic truncate.
        self.path.unlink(missing_ok=True)
        return out

    def size(self) -> int:
        if not self.path.is_file():
            return 0
        return sum(1 for line in self.path.read_text().splitlines() if line.strip())
