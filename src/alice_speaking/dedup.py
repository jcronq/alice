"""Envelope timestamp dedup.

signal-cli can redeliver messages on reconnect; signal-daemon.log can have
duplicate lines after certain recovery paths. The byte offset tracks
position in the log, but a second independent check on the envelope timestamp
is a cheap defense against replay.

Kept small and bounded: last N timestamps in memory, mirrored to disk so dedup
survives restarts.
"""

from __future__ import annotations

import pathlib
from collections import deque


class DedupStore:
    def __init__(self, path: pathlib.Path, capacity: int = 1000) -> None:
        self.path = path
        self.capacity = capacity
        self._seen: set[int] = set()
        self._order: deque[int] = deque(maxlen=capacity)
        self._load()

    def seen(self, timestamp: int) -> bool:
        return timestamp in self._seen

    def mark(self, timestamp: int) -> None:
        if timestamp in self._seen:
            return
        if len(self._order) == self.capacity:
            evicted = self._order.popleft()
            self._seen.discard(evicted)
        self._order.append(timestamp)
        self._seen.add(timestamp)
        self._append_to_disk(timestamp)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        for raw in self.path.read_text().splitlines()[-self.capacity :]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ts = int(raw)
            except ValueError:
                continue
            if ts not in self._seen:
                self._seen.add(ts)
                self._order.append(ts)

    def _append_to_disk(self, timestamp: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(f"{timestamp}\n")
        # Compact occasionally so the file doesn't grow unbounded.
        if len(self._order) >= self.capacity:
            self._compact()

    def _compact(self) -> None:
        if not self.path.is_file():
            return
        # Only keep the current in-memory window.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(str(ts) for ts in self._order) + "\n")
        tmp.replace(self.path)
