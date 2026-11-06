"""Server-Sent Events for the status site (issue #639, slice-1).

A tiny, stdlib-only SSE stack. One ``SectionBroadcaster`` process-wide,
one ``_Subscriber`` per open ``GET /api/events`` connection. The
broadcaster re-renders each section's markdown on a short polling
tick; when the rendered markdown changes, a ``section.update`` event
lands in every subscriber's queue. Snapshot-first: the very first
message on every connection is a ``type: snapshot`` payload carrying
every section's current markdown, so the client never has to reconcile
a gap (matches the connectivity guide pinned on #639 and AG-UI's
snapshot-first shape).

Wire framing is deliberately minimal — each event is a single
``data: <JSON>\\n\\n`` line, with no ``event:`` name and no ``id:``.
Event type lives inside the JSON (``"type": "snapshot"`` /
``"section.update"``). Heartbeat comments ``: ping\\n\\n`` keep the
client's idle-timer armed; the interval is injectable so tests can
pin a sub-second cadence.

Why stdlib-only: ``src/status_site/server.py`` runs on
``python:3.12-alpine`` with no third-party packages. The design doc's
``@ag-ui/encoder`` note is aspirational; the wire format it would emit
for our event set is just typed JSON over SSE, so a hand-rolled
encoder suffices.
"""

from __future__ import annotations

import hmac
import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


# ─── defaults ────────────────────────────────────────────────────────

# 15s matches the connectivity guide and is a comfortable fit for most
# reverse proxies' idle cutoffs.
DEFAULT_HEARTBEAT_SECONDS = 15.0

# Fast enough to feel live (within a tick of the cache refresh)
# without burning cycles when nothing is changing. Renders are cheap:
# each closure just formats already-polled dicts / reads a couple of
# small files.
DEFAULT_TICK_SECONDS = 2.0

# Per-subscriber mailbox cap. A slow client that fills this gets
# closed — better to drop one laggy consumer than block the broadcaster.
DEFAULT_SUBSCRIBER_QUEUE_MAX = 256


# ─── types ───────────────────────────────────────────────────────────


@dataclass(eq=False)
class _Subscriber:
    """One open SSE connection. ``closed`` flips when the handler
    unsubscribes (client disconnect, broadcaster stop) so the publish
    side can skip dead sockets without needing to poke the queue.

    ``eq=False`` keeps default identity-based hashing + equality so a
    ``_Subscriber`` can live in a ``set`` without manually defining
    ``__hash__`` on an otherwise-mutable record.
    """

    q: "queue.Queue[dict]"
    closed: threading.Event = field(default_factory=threading.Event)


# ─── broadcaster ─────────────────────────────────────────────────────


class SectionBroadcaster:
    """Re-renders sections on a short tick, fans deltas out to subscribers.

    Not coupled to ``OverviewPollers``: the broadcaster takes a dict of
    ``{section_name: () -> markdown}`` closures. The caller composes
    those from the section-render module's output (see
    ``server.section_renderers``). Cheap sources (waiting_on_you,
    alice_queue, known_issues) have no cache, so the tick is their
    "poller"; expensive sources (running_now, review_queue, done_today)
    are already kept warm by ``OverviewPollers`` — the tick just picks
    up whatever's currently cached.
    """

    def __init__(
        self,
        # ``Mapping`` (not ``dict``) so a renderer that returns ``dict``
        # — the slice-2 typed-payload shape — isn't rejected by dict
        # invariance against the legacy ``dict | str`` return. See
        # ``_safe_render`` for the str→dict backward-compat coercion.
        renderers: "Mapping[str, Callable[[], dict | str]]",
        *,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        stop: threading.Event | None = None,
        subscriber_queue_max: int = DEFAULT_SUBSCRIBER_QUEUE_MAX,
    ):
        self._renderers = dict(renderers)
        self._tick_seconds = tick_seconds
        self.stop = stop or threading.Event()
        self._subscriber_queue_max = subscriber_queue_max
        # Broadcaster-local lock. Deliberately NOT ``cfg.lock``: a
        # renderer closure itself acquires ``cfg.lock`` indirectly (via
        # ``OverviewPollers.get``); nesting the two would deadlock since
        # ``threading.Lock`` is non-reentrant.
        self._lock = threading.Lock()
        # Payloads are dicts after slice-2 — renderers return a typed
        # ``{"markdown": ..., "rows": ...}`` shape so widgets can render
        # without re-parsing markdown. Dict ``==`` in Python is deep, so
        # the change-detection compare still works as before.
        self._rendered: dict[str, dict] = {}
        self._subs: "set[_Subscriber]" = set()
        self._thread: threading.Thread | None = None

    # ── subscriber management ─────────────────────────────────────

    def subscribe(self) -> _Subscriber:
        """Register a new subscriber and return its mailbox."""
        sub = _Subscriber(q=queue.Queue(maxsize=self._subscriber_queue_max))
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        """Remove a subscriber. Idempotent."""
        sub.closed.set()
        with self._lock:
            self._subs.discard(sub)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # ── snapshot / tick ───────────────────────────────────────────

    def snapshot(self) -> dict:
        """Render every section fresh and return a ``type: snapshot``
        payload. Side-effect: the "last rendered" cache is seeded, so
        the very next tick won't re-emit everything as a delta.
        """
        sections: dict[str, dict] = {}
        rendered_now: dict[str, dict] = {}
        for name, fn in self._renderers.items():
            payload = _safe_render(fn)
            sections[name] = payload
            rendered_now[name] = payload
        with self._lock:
            self._rendered.update(rendered_now)
        return {"type": "snapshot", "ts": _now_iso(), "sections": sections}

    def tick(self) -> list[dict]:
        """Rerender each section; publish + return deltas for changes.

        Returns the list of ``section.update`` events emitted on this
        tick. Mostly useful for tests that want to synchronously drive
        the broadcast without waiting on the background thread.
        """
        events: list[dict] = []
        for name, fn in self._renderers.items():
            payload = _safe_render(fn)
            with self._lock:
                prev = self._rendered.get(name)
                if prev == payload:
                    continue
                self._rendered[name] = payload
            events.append(
                {
                    "type": "section.update",
                    "name": name,
                    "ts": _now_iso(),
                    "data": payload,
                }
            )
        for ev in events:
            self._publish(ev)
        return events

    def _publish(self, ev: dict) -> None:
        with self._lock:
            targets = list(self._subs)
        dead: list[_Subscriber] = []
        for s in targets:
            if s.closed.is_set():
                dead.append(s)
                continue
            try:
                s.q.put_nowait(ev)
            except queue.Full:
                # Slow consumer: mark dead and reap below. Better to
                # drop one lagging tab than let the broadcaster stall.
                s.closed.set()
                dead.append(s)
        if not dead:
            return
        with self._lock:
            for s in dead:
                self._subs.discard(s)

    # ── background loop ───────────────────────────────────────────

    def _loop(self) -> None:
        # First tick: don't broadcast anything, just prime ``_rendered``
        # so a fresh client's snapshot and the first delta aren't the
        # same payload. The handler's snapshot() call does this too, but
        # the loop may run before any client connects.
        for name, fn in self._renderers.items():
            with self._lock:
                self._rendered.setdefault(name, _safe_render(fn))
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — never kill the loop
                print(f"status_site: SSE tick failed: {e!r}", flush=True)
            self.stop.wait(self._tick_seconds)

    def start(self) -> None:
        """Spawn the broadcast thread. Idempotent."""
        if self._thread is not None:
            return
        t = threading.Thread(target=self._loop, name="sse-broadcast", daemon=True)
        t.start()
        self._thread = t


# ─── pump ────────────────────────────────────────────────────────────


HEARTBEAT = b": ping\n\n"


def encode_event(obj: dict) -> bytes:
    """Encode a dict as a single ``data: <JSON>\\n\\n`` SSE frame."""
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return ("data: " + payload + "\n\n").encode("utf-8")


def stream_events(
    wfile,
    broadcaster: SectionBroadcaster,
    *,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Pump SSE events to ``wfile``. Returns when the client disconnects
    or the broadcaster stops.

    The first frame is always a fresh ``snapshot``. After that the
    pump reads from the subscriber queue, writing one ``data:`` frame
    per event and a ``: ping`` heartbeat when the queue is idle for
    ``heartbeat_seconds``. Any write error (broken pipe, reset) is
    the exit signal — the client has gone away.
    """
    sub = broadcaster.subscribe()
    try:
        snap = broadcaster.snapshot()
        if not _write(wfile, encode_event(snap)):
            return
        deadline = now() + heartbeat_seconds
        while not broadcaster.stop.is_set() and not sub.closed.is_set():
            timeout = max(0.0, deadline - now())
            try:
                ev = sub.q.get(timeout=timeout)
            except queue.Empty:
                ev = None
            if ev is None:
                if not _write(wfile, HEARTBEAT):
                    return
                deadline = now() + heartbeat_seconds
                continue
            if not _write(wfile, encode_event(ev)):
                return
    finally:
        broadcaster.unsubscribe(sub)


# ─── helpers ─────────────────────────────────────────────────────────


def check_key(cfg_key: str, query_k: str) -> bool:
    """Constant-time compare of an ``?k=<key>`` query param.

    EventSource can't set headers, so the key has to ride in the URL.
    Same HMAC-compare semantics as the ``handle_reply`` body check.
    """
    if not query_k or not cfg_key:
        return False
    return hmac.compare_digest(query_k.encode(), cfg_key.encode())


def _safe_render(fn: "Callable[[], dict | str]") -> dict:
    """Run one renderer and coerce the result into a typed payload dict.

    Slice-2 switched section renderers to return ``{"markdown": ...,
    "rows": [...]}``-style dicts so widgets can render without
    re-parsing markdown. For backward compatibility we accept plain
    strings and wrap them as ``{"markdown": <str>}``; falsy returns
    (``None`` / ``{}``) become an empty-dict payload. A renderer that
    raises never kills the broadcast — we substitute an italic
    "render failed" marker and attach the repr for debugging.
    """
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001 — never kill the broadcast on one bad source
        return {"markdown": f"*(render failed: {e!r})*", "error": repr(e)}
    if out is None:
        return {}
    if isinstance(out, dict):
        return out
    if isinstance(out, str):
        return {"markdown": out}
    # Anything else: coerce to a markdown string so the widget layer
    # still has *something* to show.
    return {"markdown": str(out)}


def _write(wfile, data: bytes) -> bool:
    """Write + flush; return False if the socket died."""
    try:
        wfile.write(data)
        wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
