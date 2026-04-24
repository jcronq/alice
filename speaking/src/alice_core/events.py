"""Structured JSONL event logger and EventEmitter protocol.

One JSON record per line to an append-only log. Observability code paths
never raise — write failures are swallowed so the main loop keeps running
even when the state dir is full / unwritable / missing.

Callers write events via :class:`EventLogger`; downstream consumers (the
alice-viewer, tests, future exporters) tail the file. Tests can swap in
:class:`CapturingEmitter` to assert on the event stream without hitting
disk.

Event taxonomy lives in the hemispheres, not here — this module is
domain-agnostic. See ``alice_speaking.daemon`` and ``alice_thinking.wake``
for the event names each emits.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventEmitter(Protocol):
    """Minimal interface for event observers. Anything with ``emit(event, **fields)``
    satisfies this — EventLogger, CapturingEmitter, in-memory ring buffer, etc."""

    def emit(self, event: str, **fields: Any) -> None: ...


class EventLogger:
    """Append-only JSONL event writer.

    Each :meth:`emit` writes one line containing ``ts``, ``event``, and any
    keyword fields the caller passes. Best-effort: write failures are
    swallowed so the observability path never breaks the main loop.
    """

    def __init__(self, log_path: pathlib.Path, *, echo: bool = False) -> None:
        self.log_path = log_path
        self.echo = echo
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
            return
        if self.echo:
            # Intentional stderr write — used by --echo mode for live debugging.
            import sys
            sys.stderr.write(line + "\n")
            sys.stderr.flush()


@dataclass
class CapturingEmitter:
    """In-memory emitter for tests. Stores every emitted event in ``.events``
    as a list of ``{ts, event, **fields}`` dicts."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append({"ts": time.time(), "event": event, **fields})

    def of_kind(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]

    def clear(self) -> None:
        self.events.clear()


__all__ = ["EventEmitter", "EventLogger", "CapturingEmitter"]
