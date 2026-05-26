"""Raw-event JSONL logger for cozylobe's SSE consumer (issue #401).

We persist every event that survives the INPUT_KINDS filter as one JSON
line per event, partitioned by UTC date into
``~/alice-mind/cozylobe-cortex/events/YYYY-MM-DD.jsonl``. The corpus is
the training input for a future small self-supervised sequence model
intended to replace qwen's next-room prediction call.

Schema (one record per line)::

    {
      "ts":        ISO-8601 UTC with millisecond precision,
      "entity_id": str,
      "kind":      str,
      "state":     <raw payload dict cozyhem emitted>
    }

The logger sits AFTER the INPUT_KINDS filter (so we never log circadian
brightness ticks or other OUTPUT-class events) and BEFORE the throttle
(so duplicate "sensor still on" fires that carry useful timing
information are preserved verbatim).

Fail-safe by design: any I/O or serialization error is logged at warning
level and swallowed. The motion pipeline is the primary; logging is
secondary and must never crash the wake loop.

Naming: the unrelated :class:`core.events.EventLogger` is the daemon's
telemetry sink. To keep ``from core.events import EventLogger`` working
unmodified in :mod:`alice_cozylobe.daemon`, this corpus writer is named
:class:`SseEventLogger`.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
from datetime import datetime, timezone
from typing import IO, Callable, Optional

from .events import CozyHemEvent


__all__ = ["DEFAULT_EVENT_LOG_ROOT", "SseEventLogger"]


log = logging.getLogger(__name__)


# Default corpus root. Lives inside the alice-mind vault next to the
# existing per-room / per-sensor / per-guess directories. Bind-mounted
# into the alice container at the same path on the host.
DEFAULT_EVENT_LOG_ROOT = (
    pathlib.Path.home() / "alice-mind" / "cozylobe-cortex" / "events"
)


# Type alias for the injectable wall clock. Tests pass a closure over a
# mutable datetime so the date-roll case can be exercised without
# touching the system clock.
ClockFn = Callable[[], datetime]


def _utc_now() -> datetime:
    """Default clock — wall-clock UTC. Pulled out so tests can monkeypatch
    if they prefer that pattern over the constructor-injected clock."""
    return datetime.now(timezone.utc)


class SseEventLogger:
    """Append-only, date-partitioned JSONL writer for cozylobe SSE events.

    One file per UTC date, named ``YYYY-MM-DD.jsonl``. The file is opened
    lazily on the first :meth:`log` call (so a disabled logger never
    touches disk) and reused across calls; the date check on each write
    rotates lazily when the UTC date rolls over. :meth:`close` releases
    the descriptor on daemon shutdown.

    Construct with ``enabled=False`` to make every :meth:`log` call a
    no-op without touching the filesystem. Wired to the
    ``--no-event-log`` daemon CLI flag so we can flip the writer off if
    it ever interferes with the pipeline.

    Thread-safety: the cozylobe wake loop is single-task asyncio, so a
    lock is unnecessary for the current call site. The lock below is
    defensive — it costs ~nothing on the uncontended path and prevents
    a torn write if a future change exposes the logger to a second
    coroutine.
    """

    def __init__(
        self,
        root: pathlib.Path = DEFAULT_EVENT_LOG_ROOT,
        *,
        clock: Optional[ClockFn] = None,
        enabled: bool = True,
    ) -> None:
        self._root = pathlib.Path(root)
        self._clock: ClockFn = clock or _utc_now
        self._enabled = bool(enabled)
        self._file: Optional[IO[str]] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()
        # Whether we've already warned about a write/rotation failure
        # in the current outage. Reset on the next successful write so
        # a flapping disk produces one warning per outage, not one per
        # event.
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def log(self, event: CozyHemEvent) -> None:
        """Write one event as a JSON line. Never raises.

        See module docstring for the schema. Disabled logger → no-op.
        Anything that goes wrong on the write path is logged at warning
        level and swallowed; the motion pipeline is the primary, this
        logger is the secondary corpus.
        """
        if not self._enabled:
            return

        try:
            now = self._clock()
            date_str = now.strftime("%Y-%m-%d")
            ts_str = self._format_ts(now)
            record = {
                "ts": ts_str,
                "entity_id": event.entity_id,
                "kind": event.kind,
                # ``state`` is the raw payload cozyhem emitted. We keep
                # the full dict rather than reducing to a "state" field
                # so we don't have to re-engineer the schema later when
                # the sequence model wants additional features (e.g.
                # battery, illuminance, etc.).
                "state": event.payload,
            }
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001
            # JSON serialization errors are exotic but possible if a
            # producer ever emits a non-serializable payload. Never let
            # this kill the pipeline.
            self._warn_once(
                "serialize failed for kind=%s entity_id=%s: %s",
                event.kind,
                event.entity_id,
                exc,
            )
            return

        with self._lock:
            try:
                self._rotate_if_needed(date_str)
                if self._file is None:
                    # Open failed; _rotate_if_needed already warned.
                    return
                self._file.write(line + "\n")
                self._file.flush()
            except OSError as exc:
                self._warn_once(
                    "write failed (%s); subsequent failures silent "
                    "until recovery",
                    exc,
                )
                # Drop the descriptor so the next call retries the open
                # — handles "disk back online" gracefully.
                self._close_locked()
                return

        # Reset the warning latch on a successful write so a flapping
        # filesystem produces one warning per outage rather than one
        # per recovery.
        if self._warned:
            log.info("cozylobe event_log: writes recovered")
            self._warned = False

    def close(self) -> None:
        """Release the open file descriptor. Idempotent — safe to call
        from a daemon shutdown handler whether or not the logger ever
        opened a file."""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------
    # internals

    def _close_locked(self) -> None:
        """Close the current file. Caller MUST hold ``self._lock``."""
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                # Best-effort close; nothing we can do.
                pass
            self._file = None
        self._current_date = None

    def _rotate_if_needed(self, date_str: str) -> None:
        """Open a fresh file when the UTC date changes (or on first
        write). Caller MUST hold ``self._lock``.

        Failures here leave ``self._file = None`` so :meth:`log` sees
        the disabled state on the next call and skips the write.
        """
        if self._current_date == date_str and self._file is not None:
            return
        # Close any previous handle before re-opening.
        if self._file is not None:
            self._close_locked()
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._warn_once(
                "mkdir(%s) failed: %s; logger disabled until recovery",
                self._root,
                exc,
            )
            return
        path = self._root / f"{date_str}.jsonl"
        try:
            # ``a`` for true append-only; ``buffering=1`` keeps lines
            # flushed at newline boundaries even if a future code path
            # forgets to call flush explicitly.
            self._file = open(path, "a", encoding="utf-8", buffering=1)
            self._current_date = date_str
        except OSError as exc:
            self._warn_once(
                "open(%s) failed: %s; logger disabled until recovery",
                path,
                exc,
            )
            self._file = None
            self._current_date = None

    @staticmethod
    def _format_ts(now: datetime) -> str:
        """ISO-8601 UTC with millisecond precision and a trailing ``Z``.

        ``datetime.isoformat()`` returns microseconds with a ``+00:00``
        suffix; we trim to ms and use ``Z`` so the output matches the
        JSON-schema convention the rest of alice-mind expects.
        """
        # Force UTC even if a test injected a naive datetime by accident.
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        millis = now.microsecond // 1000
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"

    def _warn_once(self, fmt: str, *args: object) -> None:
        """Log a warning once per outage. Resets on first successful
        write so a flapping endpoint surfaces every time it recovers."""
        if self._warned:
            return
        log.warning("cozylobe event_log: " + fmt, *args)
        self._warned = True
