"""Reactive SSE event protocol for the status site.

Slice-1 of jcronq/alice#639. Provides a single GET endpoint
``/api/events`` that opens a Server-Sent-Events stream per connection,
emits a full snapshot on connect, then forwards ``section.update`` frames
whenever any source's rendered markdown changes.

Shape (per the connectivity guide at /tmp/agent-ui-connectivity-guide.md):

* Transport: SSE over fetch (the frontend client is fetch-based; this
  server emits the plain SSE wire shape).
* Recovery model: snapshot-first. Every (re)connect gets a full
  snapshot; clients never track event ids.
* Frame shape: ``data: <JSON>\\n\\n`` lines; no ``event:`` names, no
  ``id:`` lines. Event type lives inside the JSON.
* Heartbeat: a ``: ping\\n\\n`` comment line every
  ``AGENT_UI_HEARTBEAT_MS`` milliseconds (default 15000). Client's idle
  timer is three missed heartbeats (45 s) in the spec.

In-process pub/sub (:class:`EventBroker`) holds the last-rendered
markdown for each section key. The poller (:class:`SectionPoller`) runs
one background thread per key — short cadences for local reads, long
ones for sections backed by ``gh`` calls through the existing
:class:`OverviewPollers` cache. ``publish`` is a diff: identical content
is a no-op, so idle periods produce no events on the wire.

The SSE handler itself (:func:`stream_events`) is driven by the
``server.Handler``'s request thread — ``ThreadingHTTPServer`` gives us
one thread per connection, which lets us block on the subscriber queue
without stalling other requests.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from status_site.sources import (
    done_today as _done_today,
    known_issues as _known_issues,
    review_queue as _review_queue,
    running_now as _running_now,
    waiting_on_you as _waiting_on_you,
)
from status_site.sources.common import asks_path, read_asks

if TYPE_CHECKING:
    from status_site.server import Config


# Section keys shared with the client. Order mirrors how they appear on
# the current ``index.md`` Overview so the React shell can render the
# same visual sequence.
SECTION_KEYS: tuple[str, ...] = (
    "running_now",
    "ready_for_review",
    "waiting_on_you",
    "alice_queue",
    "done_today",
    "known_issues",
)


# Heartbeat cadence (milliseconds). Matches the connectivity guide.
DEFAULT_HEARTBEAT_MS = 15_000


# Per-section poll cadence (seconds). Fast where the data producer
# reads local state; slow where it has to call ``gh``. The ``gh``-backed
# sections (running_now/review_queue/done_today) already have their own
# background refresh via :class:`status_site.server.OverviewPollers`;
# the numbers below are the *markdown re-render* cadence, which is
# cheap. They exist mainly so the event poller doesn't spin every
# millisecond when nothing changed.
DEFAULT_POLL_INTERVALS_S: dict[str, float] = {
    "running_now": 15.0,
    "ready_for_review": 60.0,
    "waiting_on_you": 10.0,
    "alice_queue": 5.0,
    "done_today": 60.0,
    "known_issues": 60.0,
}


def heartbeat_ms_from_env(env: dict[str, str] | None = None) -> int:
    """Resolve ``AGENT_UI_HEARTBEAT_MS`` with a safe default + lower bound.

    Clamped to >= 500 ms so a mistyped ``0`` doesn't melt the browser.
    """
    e = dict(os.environ if env is None else env)
    raw = e.get("AGENT_UI_HEARTBEAT_MS", "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_MS
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_HEARTBEAT_MS
    return max(500, v)


# ─────────────────────────────── broker ──────────────────────────────


class EventBroker:
    """Pub/sub for rendered-markdown section updates.

    Thread-safe. ``publish`` is diff-only — identical content yields no
    event. Subscribers get their own ``queue.SimpleQueue``; the SSE
    handler drains it and writes each frame to its client.

    A fresh connect hits :meth:`snapshot` first (so the client has a
    full picture), then subscribes for the subsequent diffs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.SimpleQueue[str]] = set()
        # Pre-seeded so a connect-before-first-poll still returns a full
        # (empty-string) snapshot — the client renders each section as
        # empty until the first poll fires.
        self._state: dict[str, str] = {k: "" for k in SECTION_KEYS}

    def snapshot(self) -> dict[str, str]:
        """Return the current rendered markdown for every section key."""
        with self._lock:
            return dict(self._state)

    def publish(self, key: str, content: str) -> bool:
        """Store ``content`` under ``key`` and fan out to subscribers.

        Returns ``True`` if the content changed (an event was sent),
        ``False`` otherwise. Unknown keys are ignored (kept lenient so
        a stray poller name mismatch can't crash the service).
        """
        if key not in self._state:
            return False
        with self._lock:
            prev = self._state.get(key)
            if prev == content:
                return False
            self._state[key] = content
            subs = list(self._subscribers)
        payload = json.dumps(
            {"type": "section.update", "key": key, "content": content},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for q in subs:
            try:
                q.put_nowait(payload)
            except Exception:  # noqa: BLE001 — never let a slow consumer stall publishing
                pass
        return True

    def subscribe(self) -> queue.SimpleQueue[str]:
        q: queue.SimpleQueue[str] = queue.SimpleQueue()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.SimpleQueue[str]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# ────────────────────────── section renderers ────────────────────────
#
# Each ``render_<key>`` builds the markdown string the Overview would
# normally splice between its sentinel markers. These mirror the
# closures in :func:`status_site.server.overview_splice_sections` so the
# SSE payload is byte-for-byte what the markdown fallback shows.


def render_running_now(cfg: "Config") -> str:
    pollers = cfg.pollers
    rows = (pollers.get("running_now") if pollers else None) or []
    return _running_now.render(rows)


def render_ready_for_review(cfg: "Config") -> str:
    pollers = cfg.pollers
    cached = (pollers.get("review_queue") if pollers else None) or {}
    return _review_queue.render(
        cached.get("design", []),
        cached.get("pr", []),
        design_failed=cached.get("design_failed", []),
        pr_failed=cached.get("pr_failed", []),
    )


def render_waiting_on_you(cfg: "Config") -> str:
    return _waiting_on_you.render(
        cfg.status_dir / "waiting-on-you.md",
        asks_path=asks_path(cfg.status_dir),
    )


def render_alice_queue(cfg: "Config") -> str:
    # Local import keeps ``events`` free of the server module's heavier
    # dependency surface at import time (there's a circular if we
    # top-level-import it, since server imports events).
    from status_site.server import render_alice_queue as _render, scan_surfaces

    items = scan_surfaces(cfg.surface_dir)
    return _render(items, now=time.time())


def render_done_today(cfg: "Config") -> str:
    pollers = cfg.pollers
    cached = (pollers.get("done_today") if pollers else None) or {}
    today = cached.get("today") or _done_today.today_local()
    asks = read_asks(asks_path(cfg.status_dir))
    resolved = _done_today.resolved_today(asks, today)
    return _done_today.render(cached.get("prs", []), resolved)


def render_known_issues(cfg: "Config") -> str:
    return _known_issues.render(cfg.status_dir / "known-issues.md")


# Dispatch table. Kept module-level so tests can monkey-patch a single
# entry (e.g. force a specific key's content to trigger a diff).
SECTION_RENDERERS: dict[str, Callable[["Config"], str]] = {
    "running_now": render_running_now,
    "ready_for_review": render_ready_for_review,
    "waiting_on_you": render_waiting_on_you,
    "alice_queue": render_alice_queue,
    "done_today": render_done_today,
    "known_issues": render_known_issues,
}


def render_section(cfg: "Config", key: str) -> str:
    """Render one section's markdown. Raises ``KeyError`` on unknown."""
    return SECTION_RENDERERS[key](cfg)


# ──────────────────────────── poll driver ────────────────────────────


@dataclass
class _PollerStats:
    """Small telemetry object a test can inspect. No side effects."""
    polls: int = 0
    publishes: int = 0
    failures: int = 0


class SectionPoller:
    """One thread per section key; each polls + publishes diffs.

    Not responsible for the underlying ``gh``-backed cache refresh —
    that stays in :class:`status_site.server.OverviewPollers`. This
    class just re-renders the section markdown on its own cadence and
    pushes a diff into the broker.
    """

    def __init__(
        self,
        cfg: "Config",
        broker: EventBroker,
        *,
        intervals: dict[str, float] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.intervals = dict(DEFAULT_POLL_INTERVALS_S)
        if intervals:
            self.intervals.update(intervals)
        self.stop = stop or threading.Event()
        self._threads: list[threading.Thread] = []
        self.stats: dict[str, _PollerStats] = {k: _PollerStats() for k in SECTION_KEYS}

    def poll_once(self, key: str) -> bool:
        """Render + publish a single section. Returns ``True`` on change.

        Exposed for tests (lets a test force a render without spinning
        up the whole background loop).
        """
        try:
            content = render_section(self.cfg, key)
        except Exception as e:  # noqa: BLE001
            self.stats[key].failures += 1
            print(f"status_site events: {key} render failed: {e!r}", flush=True)
            return False
        self.stats[key].polls += 1
        changed = self.broker.publish(key, content)
        if changed:
            self.stats[key].publishes += 1
        return changed

    def _loop(self, key: str) -> None:
        interval = max(0.1, float(self.intervals.get(key, 30.0)))
        while not self.stop.is_set():
            self.poll_once(key)
            self.stop.wait(interval)

    def prime(self) -> None:
        """Do one synchronous render per key so the initial snapshot
        has real content before any subscriber connects."""
        for key in SECTION_KEYS:
            self.poll_once(key)

    def start(self) -> None:
        self.prime()
        for key in SECTION_KEYS:
            t = threading.Thread(
                target=self._loop, args=(key,), name=f"section-poll-{key}", daemon=True
            )
            t.start()
            self._threads.append(t)

    def shutdown(self, timeout: float = 2.0) -> None:
        self.stop.set()
        for t in self._threads:
            t.join(timeout=timeout)


# ────────────────────────────── wire ─────────────────────────────────


def format_frame(payload_json: str) -> bytes:
    """One SSE ``data:`` frame — no ``event:`` / ``id:``, two LFs."""
    return f"data: {payload_json}\n\n".encode("utf-8")


HEARTBEAT_FRAME = b": ping\n\n"


def snapshot_payload(broker: EventBroker) -> str:
    """JSON string for the initial ``snapshot`` event."""
    return json.dumps(
        {"type": "snapshot", "sections": broker.snapshot()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def stream_events(
    wfile,
    broker: EventBroker,
    *,
    heartbeat_ms: int = DEFAULT_HEARTBEAT_MS,
    stop: threading.Event | None = None,
    max_frames: int | None = None,
) -> int:
    """Drive one SSE connection until the peer disconnects.

    * Writes the initial ``snapshot`` frame.
    * Subscribes to ``broker`` and forwards every ``section.update``.
    * Emits ``: ping\\n\\n`` comment lines on the heartbeat cadence.
    * Returns the number of frames (data + heartbeat) that it wrote —
      exposed so tests can assert at least N of each.

    ``wfile`` is the underlying ``http.server`` ``self.wfile`` (an
    ``io.BufferedWriter`` wrapping a socket). We flush after every
    frame so the browser sees it in real time.

    ``stop`` is an optional ``threading.Event`` — a test harness can
    set it to tell this loop to exit even without a client disconnect.

    ``max_frames`` is a test-only cap on total frames written.
    """
    heartbeat_s = max(0.05, float(heartbeat_ms) / 1000.0)
    stop = stop or threading.Event()
    frames = 0

    def _write(buf: bytes) -> None:
        nonlocal frames
        wfile.write(buf)
        wfile.flush()
        frames += 1

    try:
        _write(format_frame(snapshot_payload(broker)))
    except (BrokenPipeError, ConnectionResetError, OSError):
        return frames

    q = broker.subscribe()
    try:
        while not stop.is_set():
            if max_frames is not None and frames >= max_frames:
                return frames
            try:
                msg = q.get(timeout=heartbeat_s)
            except queue.Empty:
                try:
                    _write(HEARTBEAT_FRAME)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return frames
                continue
            try:
                _write(format_frame(msg))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return frames
    finally:
        broker.unsubscribe(q)
    return frames


__all__ = [
    "DEFAULT_HEARTBEAT_MS",
    "DEFAULT_POLL_INTERVALS_S",
    "EventBroker",
    "HEARTBEAT_FRAME",
    "SECTION_KEYS",
    "SECTION_RENDERERS",
    "SectionPoller",
    "format_frame",
    "heartbeat_ms_from_env",
    "render_alice_queue",
    "render_done_today",
    "render_known_issues",
    "render_ready_for_review",
    "render_running_now",
    "render_section",
    "render_waiting_on_you",
    "snapshot_payload",
    "stream_events",
]
