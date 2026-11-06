"""Tests for ``GET /api/events`` (SSE) and the SectionBroadcaster.

Covers the slice-1 contract from issue #639: key-gated snapshot-first
SSE stream, delta-on-change semantics, heartbeat cadence, and the
``/index.md`` fallback that tests depend on. We don't exercise the
client JS — it ships as static files and the server-side wire format
is what matters.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from status_site._sse import SectionBroadcaster, encode_event
from status_site.server import (
    Config,
    make_server,
    section_renderers,
)


KEY = "sse-test-key-xyz"


# ─── fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def site(tmp_path):
    """Spin up status_site on an ephemeral port with an SSE broadcaster
    wired up but no overview pollers (cheap sources only). Short
    heartbeat + tick so timing-sensitive assertions finish quickly.
    """
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text(
        "# Overview\n\n"
        "## Running now\n"
        "<!-- running-now:start -->\n<!-- running-now:end -->\n\n"
        "## Waiting on you\n"
        "<!-- waiting-on-you:start -->\n<!-- waiting-on-you:end -->\n"
    )
    (status / "waiting-on-you.md").write_text("hand zone initial")
    (status / "known-issues.md").write_text("no known issues")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')

    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
        heartbeat_seconds=0.1,
        sse_tick_seconds=0.05,
    )
    broadcaster = SectionBroadcaster(
        section_renderers(cfg),
        tick_seconds=0.05,
    )
    cfg.broadcaster = broadcaster
    broadcaster.start()

    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield cfg, srv.server_address[1]
    finally:
        broadcaster.stop.set()
        srv.shutdown()
        srv.server_close()


# ─── helpers ─────────────────────────────────────────────────────────


def _open_sse(port: int, key: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Open a long-lived GET /api/events. Returns (conn, response).

    Uses a short socket timeout so iterating byte-by-byte won't block
    the test runner if the stream dies.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/api/events?k={key}")
    resp = conn.getresponse()
    # Short per-read timeout so the test times out in seconds, not
    # minutes, if the server stops writing.
    resp.fp.raw._sock.settimeout(3.0)
    return conn, resp


def _read_frame(resp: http.client.HTTPResponse, *, timeout: float = 3.0) -> dict | str | None:
    """Read one SSE frame (ends on blank line). Returns:

    * ``dict``   — parsed ``data:`` JSON event
    * ``str``    — "heartbeat" on ``: ping`` comments (any ``:`` comment)
    * ``None``   — connection ended without a complete frame
    """
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        try:
            chunk = resp.read(1)
        except (socket.timeout, TimeoutError):
            continue
        if not chunk:
            return None
        buf += chunk
        if buf.endswith(b"\n\n"):
            text = buf.decode("utf-8", errors="replace")
            buf = b""
            lines = text.rstrip("\n").split("\n")
            if all(l.startswith(":") for l in lines if l):
                return "heartbeat"
            data_parts = [
                l[len("data:"):].lstrip(" ")
                for l in lines
                if l.startswith("data:")
            ]
            if not data_parts:
                continue  # unknown frame — keep reading
            try:
                return json.loads("\n".join(data_parts))
            except ValueError:
                return None
    raise AssertionError(f"no complete SSE frame within {timeout}s (buffer: {buf!r})")


def _read_frames_until(
    resp: http.client.HTTPResponse,
    predicate,
    *,
    timeout: float = 3.0,
) -> dict:
    """Drain frames until ``predicate(frame)`` is True; return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = _read_frame(resp, timeout=max(0.1, deadline - time.monotonic()))
        if ev is None:
            raise AssertionError("stream ended before predicate matched")
        if predicate(ev):
            return ev
    raise AssertionError("predicate never matched in time")


def _get(port: int, path: str) -> tuple[int, bytes, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    out = r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return out


# ─── auth ────────────────────────────────────────────────────────────


def test_events_requires_key(site):
    """Missing / wrong key must 403 without opening a stream."""
    cfg, port = site
    assert _get(port, "/api/events")[0] == 403
    assert _get(port, "/api/events?k=")[0] == 403
    assert _get(port, "/api/events?k=wrong")[0] == 403
    # Prefix of valid key must not match (constant-time compare).
    assert _get(port, f"/api/events?k={KEY[:-1]}")[0] == 403


# ─── snapshot ────────────────────────────────────────────────────────


def test_first_frame_is_snapshot_with_every_section(site):
    """First SSE frame carries every Overview section's markdown."""
    cfg, port = site
    conn, resp = _open_sse(port, KEY)
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith("text/event-stream")
        assert resp.getheader("Cache-Control") == "no-cache, no-transform"
        assert resp.getheader("X-Accel-Buffering") == "no"
        # Drain the opening comment ("alice-sse v1") then expect the
        # snapshot as the first *data* frame.
        snap = _read_frames_until(resp, lambda e: isinstance(e, dict))
        assert snap["type"] == "snapshot"
        expected = {
            "running_now",
            "ready_for_review",
            "waiting_on_you",
            "alice_queue",
            "done_today",
            "known_issues",
        }
        assert expected <= set(snap["sections"].keys())
        # Each section carries markdown.
        for name in expected:
            assert "markdown" in snap["sections"][name]
        # The hand-zone text we wrote is in waiting_on_you.
        assert "hand zone initial" in snap["sections"]["waiting_on_you"]["markdown"]
    finally:
        conn.close()


# ─── delta ───────────────────────────────────────────────────────────


def test_section_update_fires_on_asks_write(site):
    """Writing a new ask should push a section.update for waiting_on_you."""
    cfg, port = site
    conn, resp = _open_sse(port, KEY)
    try:
        snap = _read_frames_until(resp, lambda e: isinstance(e, dict) and e.get("type") == "snapshot")
        assert "ping me loudly" not in snap["sections"]["waiting_on_you"]["markdown"]

        asks = cfg.status_dir / "asks.jsonl"
        asks.write_text(
            json.dumps(
                {
                    "id": "ask-1",
                    "text": "ping me loudly",
                    "created": "2026-11-06T09:00:00",
                    "resolved": False,
                }
            )
            + "\n"
        )
        # Wait for the delta. Tick is 0.05s; give it a couple of seconds.
        ev = _read_frames_until(
            resp,
            lambda e: isinstance(e, dict)
            and e.get("type") == "section.update"
            and e.get("name") == "waiting_on_you",
            timeout=4.0,
        )
        assert "ping me loudly" in ev["data"]["markdown"]
    finally:
        conn.close()


# ─── heartbeat ───────────────────────────────────────────────────────


def test_heartbeat_fires_on_idle(site):
    """With heartbeat=0.1s, an idle stream should produce a ``: ping``
    comment promptly after the snapshot."""
    cfg, port = site
    conn, resp = _open_sse(port, KEY)
    try:
        _read_frames_until(resp, lambda e: isinstance(e, dict) and e.get("type") == "snapshot")
        # Drain any immediate section.update deltas from the first
        # cold-render pass (ticks 0.05s), then expect a heartbeat.
        saw_heartbeat = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ev = _read_frame(resp, timeout=1.0)
            if ev == "heartbeat":
                saw_heartbeat = True
                break
        assert saw_heartbeat, "no heartbeat within 3s on an idle stream"
    finally:
        conn.close()


# ─── /index.md regression ───────────────────────────────────────────


def test_index_md_still_renders(site):
    """The old splice-rendered markdown path must keep working — the
    client's fallback relies on it, and existing tests downstream do too.
    """
    cfg, port = site
    status, body, _ = _get(port, "/index.md")
    assert status == 200
    assert body.startswith(b"# Overview")
    # Splice markers were present in the fixture; the waiting-on-you
    # hand zone should have been spliced in.
    assert b"hand zone initial" in body


def test_root_references_events_endpoint(site):
    """Smoke test: GET / returns the new shell and it references
    /api/events — so a browser hitting the root knows where to stream
    from.
    """
    cfg, port = site
    status, body, headers = _get(port, "/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    text = body.decode("utf-8", errors="replace")
    assert "/api/events" in text
    assert "marked.min.js" in text


def test_legacy_html_still_served(site):
    """The pre-#639 shell stays available at /legacy.html so nav tabs
    + modal + reply + chat keep working during the slice-2 port.
    """
    cfg, port = site
    status, body, headers = _get(port, "/legacy.html")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    # Nav/modal markers unique to the legacy shell.
    text = body.decode("utf-8", errors="replace")
    assert "id=\"modal\"" in text
    assert "api/threads" in text


# ─── encoder unit ───────────────────────────────────────────────────


def test_encode_event_shape():
    """Single ``data: <json>\\n\\n`` frame, no event:/id: lines."""
    out = encode_event({"type": "snapshot", "sections": {"x": {"markdown": "hi"}}})
    text = out.decode("utf-8")
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    body = text[len("data: "):-2]
    assert json.loads(body)["type"] == "snapshot"
    assert "\nevent:" not in text
    assert "\nid:" not in text


# ─── broadcaster unit ───────────────────────────────────────────────


def test_broadcaster_tick_broadcasts_only_on_change(tmp_path):
    """SectionBroadcaster.tick() publishes nothing when renders don't
    move, and one event per section when they do.
    """
    state = {"name": "first"}
    renderers = {
        "sec_a": lambda: state["name"],
        "sec_b": lambda: "constant",
    }
    b = SectionBroadcaster(renderers, tick_seconds=0.01)
    sub = b.subscribe()
    # Snapshot primes the rendered cache.
    snap = b.snapshot()
    assert snap["sections"]["sec_a"]["markdown"] == "first"
    # No change → tick emits nothing.
    assert b.tick() == []
    # Change sec_a → one delta on sec_a only.
    state["name"] = "second"
    events = b.tick()
    assert len(events) == 1
    assert events[0]["name"] == "sec_a"
    assert events[0]["data"]["markdown"] == "second"
    # Subscriber mailbox has the one delta queued.
    ev = sub.q.get_nowait()
    assert ev["name"] == "sec_a"
    b.unsubscribe(sub)
    b.stop.set()
