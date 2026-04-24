"""Structured JSONL event logger for the speaking daemon.

One JSON record per line, written with wall-clock timestamps, tailed by
the alice-viewer service to render timelines and graphs. Stdlib logging
stays for operational logs; this is the observability event stream.

Event types emitted by the daemon include (non-exhaustive):

- ``daemon_start`` / ``daemon_ready`` / ``shutdown`` — lifecycle
- ``signal_turn_start`` / ``signal_turn_end`` — per inbound message
- ``surface_dispatch`` / ``surface_turn_end`` — per surfaced thought
- ``emergency_dispatch`` / ``emergency_voiced`` / ``emergency_downgraded``
- ``assistant_text`` / ``tool_use`` / ``thinking`` / ``result`` — per turn trace
- ``signal_send`` / ``quiet_queue_enter`` / ``quiet_queue_drain`` — outbox
- ``config_reload`` — hot-reload
- ``context_bootstrap`` — Layer 2 turn_log-based restart bootstrap fired
- ``context_compaction`` — compaction turn ran; summary written
- ``session_roll`` — session_id cleared after compaction; next turn fresh
- ``session_resume_failed`` — ``resume=`` threw; cleared session and retried
- ``missed_reply`` — turn closed without a send_message call
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any


def _short(obj: Any, cap: int = 2000) -> str:
    """Truncate an arbitrary value into a short string for log fields.

    Non-strings are dumped with ``json.dumps(..., default=str)`` so the
    logger never crashes on unknown objects.
    """
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[: cap - 1] + "…"


class EventLogger:
    """Append-only JSONL event writer.

    Each ``emit`` writes one line containing ``ts``, ``event``, and any
    keyword fields the caller passes. Best-effort: write failures are
    swallowed so the observability path never breaks the main loop.
    """

    def __init__(self, log_path: pathlib.Path) -> None:
        self.log_path = log_path
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # If the state dir is unavailable, emit becomes a no-op below.
            pass

    def emit(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self.log_path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            # Observability must never break the main loop.
            pass


__all__ = ["EventLogger", "_short"]
