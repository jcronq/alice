"""AG-UI event encoding + section-scoped SSE for the reactive rewrite.

Slice-1 of #639. The existing ``_sse.py`` handles the legacy
``/api/events`` stream (snapshot / section.update over all Overview
sections) which the hand-rolled ``index.html`` reads. The new
``/events`` endpoint — read by the React+Vite bundle at
``/ui/running-now`` — speaks a trimmed AG-UI vocabulary scoped to one
section.

Why a second module instead of extending ``_sse.py``:

* The AG-UI wire format (type names in SCREAMING_SNAKE_CASE, a
  ``snapshot`` object keyed by state-tree path, reserved event types
  for future per-task progress) is a deliberately-different shape
  from the legacy section.update envelope. Mixing them in one module
  would push the type-level distinction into every call site.
* The new stream is section-scoped — a React panel subscribes to one
  slice of state and doesn't want deltas for every other section.
  The legacy broadcaster fans every delta to every subscriber.
* Later slices will port more sections. Keeping the encoder + the
  per-section broadcaster separate makes it easy to add
  ``?section=waiting_on_you`` without touching the legacy path.

Wire format recap (per the connectivity guide pinned on #639):

* ``text/event-stream``, one frame per ``data: <JSON>\\n\\n`` block.
* Event type lives inside the JSON, not on an ``event:`` line.
* ``: ping\\n\\n`` heartbeat frames keep idle connections warm.
* First frame on every (re)connect is a ``STATE_SNAPSHOT`` carrying
  the full section payload — clients never track event ids or try to
  reconcile a gap.
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

DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_TICK_SECONDS = 2.0
DEFAULT_SUBSCRIBER_QUEUE_MAX = 256

# AG-UI event type names. SCREAMING_SNAKE_CASE matches the upstream
# spec (ag-ui.dev) so a future swap to @ag-ui/encoder's TS types stays
# a one-liner on the client side.
EVT_STATE_SNAPSHOT = "STATE_SNAPSHOT"
EVT_RUN_STARTED = "RUN_STARTED"
EVT_RUN_FINISHED = "RUN_FINISHED"


# ─── subscriber ──────────────────────────────────────────────────────


@dataclass(eq=False)
class _Subscriber:
    """One open SSE connection, scoped to one section name.

    ``eq=False`` keeps identity-based hashing so instances live in a
    ``set`` without defining ``__hash__`` on a mutable record.
    """

    section: str
    q: "queue.Queue[dict]"
    closed: threading.Event = field(default_factory=threading.Event)


# ─── broadcaster ─────────────────────────────────────────────────────


class AgUiBroadcaster:
    """Per-section change-detector + fan-out.

    Takes a mapping of ``{section_name: () -> payload}`` and re-renders
    on a short tick. When a payload changes, ``STATE_SNAPSHOT`` events
    land in every subscriber whose ``section`` matches. Lists are
    replaced wholesale (no JSON Patch) — the connectivity guide pins
    this and it's the whole point of snapshot-first.

    Multi-section subscriptions aren't a goal of slice-1; the client
    opens one connection per section it cares about. Keeping the API
    section-scoped also makes the subscriber-mailbox back-pressure
    tractable: a slow panel only blocks its own mailbox.
    """

    def __init__(
        self,
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
        self._lock = threading.Lock()
        self._rendered: dict[str, dict] = {}
        self._subs: "set[_Subscriber]" = set()
        self._thread: threading.Thread | None = None

    # ── subscriber management ─────────────────────────────────────

    def subscribe(self, section: str) -> _Subscriber:
        """Register a new section-scoped subscriber."""
        sub = _Subscriber(
            section=section,
            q=queue.Queue(maxsize=self._subscriber_queue_max),
        )
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        """Remove a subscriber. Idempotent."""
        sub.closed.set()
        with self._lock:
            self._subs.discard(sub)

    def subscriber_count(self, section: str | None = None) -> int:
        with self._lock:
            if section is None:
                return len(self._subs)
            return sum(1 for s in self._subs if s.section == section)

    def has_section(self, name: str) -> bool:
        return name in self._renderers

    def sections(self) -> list[str]:
        return list(self._renderers.keys())

    # ── snapshot / tick ───────────────────────────────────────────

    def snapshot(self, section: str) -> dict:
        """Render one section fresh and return a ``STATE_SNAPSHOT``
        event. Side-effect: ``_rendered[section]`` is seeded so the
        next tick won't re-emit this as a delta.
        """
        fn = self._renderers.get(section)
        payload = _safe_render(fn) if fn is not None else {}
        with self._lock:
            self._rendered[section] = payload
        return _state_snapshot(section, payload)

    def tick(self) -> list[dict]:
        """Rerender each section; publish + return any ``STATE_SNAPSHOT``
        events emitted. Useful for tests that drive the broadcast
        synchronously.
        """
        events: list[dict] = []
        for name, fn in self._renderers.items():
            payload = _safe_render(fn)
            with self._lock:
                prev = self._rendered.get(name)
                if prev == payload:
                    continue
                self._rendered[name] = payload
            events.append(_state_snapshot(name, payload))
        for ev in events:
            self._publish(ev)
        return events

    def _publish(self, ev: dict) -> None:
        section = ev.get("_section") or ""
        with self._lock:
            targets = [s for s in self._subs if s.section == section]
        dead: list[_Subscriber] = []
        for s in targets:
            if s.closed.is_set():
                dead.append(s)
                continue
            try:
                s.q.put_nowait(ev)
            except queue.Full:
                # Slow consumer: mark dead; don't block the broadcast.
                s.closed.set()
                dead.append(s)
        if not dead:
            return
        with self._lock:
            for s in dead:
                self._subs.discard(s)

    # ── background loop ───────────────────────────────────────────

    def _loop(self) -> None:
        # Prime the rendered cache so the first tick after startup
        # won't look like "every section changed" to subscribers who
        # already got their snapshot.
        for name, fn in self._renderers.items():
            with self._lock:
                self._rendered.setdefault(name, _safe_render(fn))
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — never kill the loop
                print(f"status_site: AG-UI tick failed: {e!r}", flush=True)
            self.stop.wait(self._tick_seconds)

    def start(self) -> None:
        """Spawn the broadcast thread. Idempotent."""
        if self._thread is not None:
            return
        t = threading.Thread(
            target=self._loop, name="ag-ui-broadcast", daemon=True
        )
        t.start()
        self._thread = t


# ─── pump ────────────────────────────────────────────────────────────


HEARTBEAT = b": ping\n\n"


def encode_event(obj: dict) -> bytes:
    """Encode a dict as a single ``data: <JSON>\\n\\n`` SSE frame.

    The outbound JSON strips the private ``_section`` routing key so
    the client sees only the AG-UI-shaped event. ``_section`` is
    an internal label the broadcaster uses to route to the right
    per-section mailbox; it is not part of the AG-UI wire format.
    """
    public = {k: v for k, v in obj.items() if not k.startswith("_")}
    payload = json.dumps(public, separators=(",", ":"), ensure_ascii=False)
    return ("data: " + payload + "\n\n").encode("utf-8")


def stream_events(
    wfile,
    broadcaster: AgUiBroadcaster,
    section: str,
    *,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Pump AG-UI events for ``section`` to ``wfile``.

    First frame is always a ``STATE_SNAPSHOT``. Subsequent frames are
    ``STATE_SNAPSHOT`` events whenever the server-side change detector
    fires; idle periods get ``: ping`` heartbeats every
    ``heartbeat_seconds``. Any write error exits the pump — the
    client has gone away.
    """
    sub = broadcaster.subscribe(section)
    try:
        snap = broadcaster.snapshot(section)
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
    """Constant-time compare for the ``?k=<key>`` query param.

    Same shape as ``_sse.check_key`` so operators don't have to learn
    a second auth model for the new stream. Pulled out as its own
    function so the call site stays readable.
    """
    if not query_k or not cfg_key:
        return False
    return hmac.compare_digest(query_k.encode(), cfg_key.encode())


def _state_snapshot(section: str, payload: dict) -> dict:
    """Shape one ``STATE_SNAPSHOT`` event for ``section``.

    ``_section`` is an internal routing label stripped by
    :func:`encode_event` before the frame goes over the wire.
    """
    return {
        "type": EVT_STATE_SNAPSHOT,
        "ts": _now_iso(),
        "snapshot": {section: payload},
        "_section": section,
    }


def _safe_render(fn: "Callable[[], dict | str] | None") -> dict:
    """Run one renderer and coerce the result into a typed payload dict.

    Mirrors ``_sse._safe_render`` so renderers written for the legacy
    stream work unchanged on the AG-UI stream. A renderer that raises
    yields a ``{"markdown": "*(render failed: …)*"}`` payload — never
    propagates, so one bad section can't kill the stream.
    """
    if fn is None:
        return {}
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001 — never kill the broadcast
        return {"markdown": f"*(render failed: {e!r})*", "error": repr(e)}
    if out is None:
        return {}
    if isinstance(out, dict):
        return out
    if isinstance(out, str):
        return {"markdown": out}
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
