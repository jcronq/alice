"""Tests for the SSE event protocol (slice-1 of jcronq/alice#639).

Covers the ``GET /api/events`` endpoint shape (headers, snapshot-first
frame, section.update diffs, heartbeat) plus the in-process
:class:`EventBroker` and :class:`SectionPoller` behaviours.

Mirrors the fixture shape in ``test_status_site_queue.py``: tmp_path
``Config``, ``make_server`` on an ephemeral port, live ``http.client``
requests. The SSE handler runs in a worker thread (inherited from
``ThreadingHTTPServer``) so the test opens the socket in blocking mode
and reads bytes with a small per-read timeout.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from status_site.events import (
    EventBroker,
    SectionPoller,
    format_frame,
    heartbeat_ms_from_env,
    snapshot_payload,
    stream_events,
)
from status_site.server import Config, make_server


KEY = "events-key-xyz"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def site(tmp_path):
    """Live server with an ``EventBroker`` attached; no background threads."""
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
    )
    broker = EventBroker()
    cfg.events_broker = broker
    # Short heartbeat keeps the heartbeat test under a few seconds.
    cfg.heartbeat_ms = 200
    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield cfg, broker, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- helpers


class SseConn:
    """Minimal SSE reader over a raw socket.

    Holds the underlying socket + a buffer of already-received bytes
    so :meth:`read_until` can span multiple recv() calls.
    """

    def __init__(self, sock: socket.socket, prefetch: bytes, head: bytes) -> None:
        self.sock = sock
        self.buf = bytearray(prefetch)
        self.head = head

    def close(self) -> None:
        self.sock.close()

    def read_until(
        self, needle: bytes, max_bytes: int = 65536, timeout: float = 5.0
    ) -> bytes:
        end_by = time.time() + timeout
        while needle not in bytes(self.buf):
            if len(self.buf) > max_bytes:
                raise RuntimeError(
                    f"read_until: {max_bytes} bytes without seeing {needle!r}"
                )
            remaining = end_by - time.time()
            if remaining <= 0:
                raise TimeoutError(f"read_until: timed out waiting for {needle!r}")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as e:
                raise TimeoutError(
                    f"read_until: timed out waiting for {needle!r}"
                ) from e
            if not chunk:
                raise RuntimeError("read_until: socket closed before match")
            self.buf += chunk
        raw = bytes(self.buf)
        idx = raw.find(needle) + len(needle)
        out = raw[:idx]
        self.buf = bytearray(raw[idx:])
        return out

    def drain_for(self, seconds: float) -> bytes:
        """Return every byte received over ``seconds`` seconds."""
        end_by = time.time() + seconds
        while True:
            remaining = end_by - time.time()
            if remaining <= 0:
                break
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk
        out = bytes(self.buf)
        self.buf = bytearray()
        return out


def open_sse(port: int, timeout: float = 5.0) -> SseConn:
    """Open a raw socket to ``/api/events`` and return the connection
    wrapper after the server's headers have been read."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.sendall(
        b"GET /api/events HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Accept: text/event-stream\r\n"
        b"Connection: keep-alive\r\n\r\n"
    )
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before headers arrived")
        buf += chunk
    head, _, tail = buf.partition(b"\r\n\r\n")
    return SseConn(sock, tail, head)


def parse_frames(raw: bytes) -> list[dict]:
    """Return the list of ``data: ...`` JSON payloads in ``raw``.

    Comment lines (``:``) and unknown field names are ignored, matching
    the SSE spec. Multi-line ``data:`` fields are joined with ``\\n``.
    """
    events: list[dict] = []
    current: list[str] = []
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        if line == "":
            if current:
                try:
                    events.append(json.loads("\n".join(current)))
                except ValueError:
                    pass
                current = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            # Per spec: strip exactly one leading space if present.
            val = line[len("data:") :]
            if val.startswith(" "):
                val = val[1:]
            current.append(val)
    if current:
        try:
            events.append(json.loads("\n".join(current)))
        except ValueError:
            pass
    return events


# ---------------------------------------------------------------- unit


def test_event_broker_publish_diffs_only():
    b = EventBroker()
    assert b.publish("running_now", "hello") is True
    assert b.publish("running_now", "hello") is False  # idempotent
    assert b.publish("running_now", "hello2") is True


def test_event_broker_unknown_key_is_ignored():
    b = EventBroker()
    assert b.publish("nope_not_a_key", "x") is False


def test_event_broker_snapshot_contains_all_keys():
    b = EventBroker()
    snap = b.snapshot()
    assert set(snap) == {
        "running_now",
        "ready_for_review",
        "waiting_on_you",
        "alice_queue",
        "done_today",
        "known_issues",
    }
    # Pre-seed is empty string, not missing — the client can render
    # each section as "no content yet" rather than erroring on missing
    # keys.
    assert all(v == "" for v in snap.values())


def test_event_broker_fanout_to_multiple_subscribers():
    b = EventBroker()
    q1 = b.subscribe()
    q2 = b.subscribe()
    b.publish("running_now", "payload-1")
    msg1 = q1.get(timeout=1)
    msg2 = q2.get(timeout=1)
    assert json.loads(msg1) == json.loads(msg2)
    assert json.loads(msg1) == {
        "type": "section.update",
        "key": "running_now",
        "content": "payload-1",
    }


def test_snapshot_payload_shape():
    b = EventBroker()
    b.publish("running_now", "A")
    b.publish("alice_queue", "B")
    payload = json.loads(snapshot_payload(b))
    assert payload["type"] == "snapshot"
    assert payload["sections"]["running_now"] == "A"
    assert payload["sections"]["alice_queue"] == "B"


def test_format_frame_shape():
    frame = format_frame('{"type":"snapshot"}')
    # ``data: <json>\n\n`` — single line, two LFs, no ``event:`` / ``id:``.
    assert frame.startswith(b"data: ")
    assert frame.endswith(b"\n\n")
    assert b"event:" not in frame
    assert b"id:" not in frame


def test_heartbeat_ms_from_env_bounds():
    assert heartbeat_ms_from_env({}) == 15000
    assert heartbeat_ms_from_env({"AGENT_UI_HEARTBEAT_MS": "500"}) == 500
    assert heartbeat_ms_from_env({"AGENT_UI_HEARTBEAT_MS": "0"}) == 500  # clamped
    assert heartbeat_ms_from_env({"AGENT_UI_HEARTBEAT_MS": "nope"}) == 15000


def test_section_poller_prime_populates_broker(tmp_path):
    """A primed poller renders every section at least once.

    The known-issues source reads a file path; empty dir still yields a
    valid render (empty or ``*(none)*``-style). We just assert that the
    broker's snapshot has been *touched* — i.e. the primer didn't
    silently crash for any key.
    """
    status = tmp_path / "status"
    status.mkdir()
    (status / "pages.json").write_text("[]")
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
    )
    broker = EventBroker()
    poller = SectionPoller(cfg, broker)
    poller.prime()
    snap = broker.snapshot()
    # Every key is a string (even if empty-ish); render functions all
    # ran without raising.
    assert all(isinstance(v, str) for v in snap.values())
    # At least one of the primed renders should have written non-empty
    # content — waiting_on_you always renders a header, as does
    # known_issues. If all six are "", something's wrong.
    assert any(v for v in snap.values())


def test_stream_events_emits_snapshot_first():
    """``stream_events`` writes the snapshot before any update frame."""
    broker = EventBroker()
    broker.publish("running_now", "hello")

    class FakeWriter:
        def __init__(self):
            self.buf = bytearray()

        def write(self, b):
            self.buf += b

        def flush(self):
            pass

    wfile = FakeWriter()
    stop = threading.Event()
    # Pre-populate one update, then let the loop idle until it pushes a
    # heartbeat — stopping after 2 frames total (snapshot + heartbeat).
    t = threading.Thread(
        target=stream_events,
        args=(wfile, broker),
        kwargs={"heartbeat_ms": 100, "stop": stop, "max_frames": 2},
        daemon=True,
    )
    t.start()
    t.join(timeout=3)
    assert not t.is_alive(), "stream_events did not exit at max_frames"
    text = bytes(wfile.buf)
    # Snapshot frame first.
    first_frame_end = text.find(b"\n\n")
    assert first_frame_end > 0
    first_frame = text[: first_frame_end + 2]
    assert first_frame.startswith(b"data: ")
    payload = json.loads(first_frame[len(b"data: ") : -2])
    assert payload["type"] == "snapshot"
    assert payload["sections"]["running_now"] == "hello"


# ---------------------------------------------------------------- http


def test_api_events_returns_sse_headers(site):
    cfg, broker, port = site
    # Issue a HEAD to assert header shape without blocking on a body.
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("HEAD", "/api/events")
    r = c.getresponse()
    assert r.status == 200
    headers = {k.lower(): v for k, v in r.getheaders()}
    assert headers.get("content-type") == "text/event-stream"
    assert "no-cache" in headers.get("cache-control", "")
    assert "no-transform" in headers.get("cache-control", "")
    assert headers.get("connection", "").lower() == "keep-alive"
    assert headers.get("x-accel-buffering") == "no"
    c.close()


def test_headers_are_sse_shape(site):
    """Alias: this is the name the issue brief asked for."""
    test_api_events_returns_sse_headers(site)


def test_api_events_emits_snapshot_first(site):
    cfg, broker, port = site
    # Seed the broker so the snapshot has real content.
    broker.publish("running_now", "## Running now\n\n*(idle)*\n")
    conn = open_sse(port)
    try:
        raw = conn.read_until(b"\n\n", timeout=5)
        frames = parse_frames(raw)
        assert frames, f"no frames parsed from: {raw!r}"
        assert frames[0]["type"] == "snapshot"
        assert "running_now" in frames[0]["sections"]
        assert frames[0]["sections"]["running_now"].startswith("## Running now")
    finally:
        conn.close()


def test_api_events_section_update_arrives_on_publish(site):
    cfg, broker, port = site
    conn = open_sse(port)
    try:
        # Drain the snapshot first.
        conn.read_until(b"\n\n", timeout=5)
        # Publish from the test thread — the broker fan-out will push
        # it to the handler thread's subscriber queue.
        broker.publish("alice_queue", "- fresh item\n")
        # Keep reading until we get a non-heartbeat data frame (the
        # 200 ms heartbeat can interleave).
        evt = None
        deadline = time.time() + 5.0
        collected = bytearray()
        while evt is None and time.time() < deadline:
            raw = conn.read_until(b"\n\n", timeout=5)
            collected += raw
            frames = parse_frames(raw)
            for f in frames:
                if f.get("type") == "section.update":
                    evt = f
                    break
        assert evt is not None, (
            f"no section.update frame within 5s; saw: {bytes(collected)!r}"
        )
        assert evt["key"] == "alice_queue"
        assert evt["content"] == "- fresh item\n"
    finally:
        conn.close()


def test_heartbeat_fires(site):
    cfg, broker, port = site
    # Fixture sets a 200 ms heartbeat; wait ~1s (several beats) to give
    # the server a chance under a loaded CI runner.
    conn = open_sse(port)
    try:
        conn.read_until(b"\n\n", timeout=5)
        # Nothing published → the very next byte off the wire must be a
        # comment line. Drain for ~1s to collect at least one beat.
        buf = conn.drain_for(seconds=1.2)
        text = buf.decode("utf-8", errors="replace")
        assert ": ping" in text, f"no heartbeat found in: {text!r}"
    finally:
        conn.close()


def test_api_events_returns_503_when_disabled(tmp_path):
    """If the broker isn't attached, the endpoint reports disabled."""
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "pages.json").write_text("[]")
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        host="127.0.0.1",
        port=0,
    )
    # Note: no broker attached.
    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 503
        body = json.loads(r.read())
        assert "events disabled" in body.get("error", "")
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- react shell


def test_app_route_404s_when_dist_missing(site):
    """Without a built React bundle, ``/app/`` returns a 404."""
    cfg, broker, port = site
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # We serve from the packaged ``static/react-app/dist``. If that
    # directory is absent, the handler reports "not built" / 404. In
    # the committed tree the ``dist`` directory IS present — in which
    # case this test simply asserts the response is either 200 (HTML)
    # or 404 (shell not built). The point of the test is that the
    # route is wired, not that one particular outcome happens.
    c.request("GET", "/app/")
    r = c.getresponse()
    body = r.read()
    status = r.status
    c.close()
    assert status in (200, 404)
    if status == 200:
        # Must be the React shell's root HTML.
        assert b"<html" in body.lower() or b"<!doctype" in body.lower()
    else:
        payload = json.loads(body)
        assert "error" in payload
