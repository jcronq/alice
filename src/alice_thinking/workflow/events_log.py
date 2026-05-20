"""events.jsonl reader/writer for thinking.workflow.

Append-only log; one JSON line per event. :func:`append_event` holds an
``fcntl.flock(LOCK_EX)`` over the file across "read last event_id → write
new line → fsync" so concurrent appenders (Speaking writing while a
harness wake is reading) can't interleave a half-written line or
duplicate an event_id.

Reads are lock-free: a partially-written final line can only occur if a
writer crashed between ``write`` and ``fsync``. In practice the flock
makes that a write-then-crash inside the critical section, which we
treat as "tolerable": :func:`read_events` skips JSON-decode errors on
the trailing line (logged at higher layers if desired).
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Optional

from alice_thinking.workflow.schema import (
    WorkflowEvent,
    event_from_dict,
    event_to_dict,
)


__all__ = [
    "append_event",
    "read_events",
    "read_last_event_id",
]


def _ensure_parent(path: Path) -> None:
    """Create the parent directory if it doesn't exist."""
    path.parent.mkdir(parents=True, exist_ok=True)


def read_last_event_id(path: Path) -> Optional[int]:
    """Return the largest ``event_id`` in the log, or None if empty/missing.

    Scans the whole file — the V1 log is expected to stay small (< 10k
    events). When it grows, swap this for a true seek-from-end tail
    scanner.
    """
    if not path.is_file():
        return None
    last: Optional[int] = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Tolerate a torn trailing write; ignore the malformed
                # line rather than crashing the read path.
                continue
            try:
                event_id = int(data["event_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if last is None or event_id > last:
                last = event_id
    return last


def append_event(path: Path, event: WorkflowEvent) -> int:
    """Append ``event`` to ``path`` under flock. Returns the assigned event_id.

    The ``event_id`` field on the input is ignored: we assign
    ``max(existing) + 1`` (or ``1`` if the log is empty) inside the
    locked critical section so concurrent appenders can't collide.

    Writes are line-flushed and fsync'd before the lock releases.
    """
    _ensure_parent(path)
    # Open r+ (or create empty if missing) so we can read the tail and
    # then append in the same handle, sharing one flock.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Read the existing content to find the current max event_id.
        # We re-read inside the lock because some other process may
        # have appended since the path existed.
        os.lseek(fd, 0, os.SEEK_SET)
        existing = os.read(fd, os.fstat(fd).st_size).decode("utf-8")
        current_max = 0
        for line in existing.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                event_id = int(data["event_id"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if event_id > current_max:
                current_max = event_id
        new_event_id = current_max + 1
        # Rebuild the event with the assigned id and serialize.
        record = event_to_dict(event)
        record["event_id"] = new_event_id
        line = json.dumps(record, sort_keys=True) + "\n"
        # Seek to end and write. (lseek to end is required because the
        # earlier read moved the file position to whatever we read.)
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return new_event_id


def read_events(
    path: Path, since_event_id: Optional[int] = None
) -> Iterator[WorkflowEvent]:
    """Stream events from the log.

    If ``since_event_id`` is set, only yields events with
    ``event_id > since_event_id``. If the file is missing, yields
    nothing. Lines that fail to JSON-decode are silently skipped (see
    the module docstring on torn writes).
    """
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                event = event_from_dict(data)
            except (KeyError, ValueError):
                continue
            if since_event_id is not None and event.event_id <= since_event_id:
                continue
            yield event
