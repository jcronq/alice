"""Tests for the AG-UI SSE stream + React bundle serving (issue #639,
slice-1).

Covers:

* ``GET /events`` — key auth, section allowlist, snapshot-first shape,
  AG-UI event envelope (SCREAMING_SNAKE_CASE ``type``, ``snapshot``
  keyed by section name), delta-on-change (second STATE_SNAPSHOT fires
  when the running_now payload changes).
* ``GET /ui/running-now`` — serves the committed Vite bundle's
  ``index.html``; ``GET /ui/assets/<file>`` serves fingerprinted JS;
  path-traversal attempts 404.
* Encoder unit tests — ``_section`` routing label is stripped from the
  wire format, malformed renderer doesn't kill the stream.

We deliberately don't hit real git / ps / inner/tasks — the running_now
renderer is injected via a mock closure so the test runs deterministically
in CI.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from status_site._ag_ui import (
    AgUiBroadcaster,
    check_key,
    encode_event,
)
from status_site.server import (
    Config,
    make_server,
)


KEY = "ag-ui-test-key-abc"


# ─── fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def site(tmp_path):
    """Spin up status_site on an ephemeral port with an AG-UI
    broadcaster wired up. We inject a one-renderer map so the tick
    only sees ``running_now`` — keeping the test fast + deterministic.
    """
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text(
        "# Overview\n\n"
        "## Running now\n"
        "<!-- running-now:start -->\n<!-- running-now:end -->\n"
    )
    (status / "waiting-on-you.md").write_text("initial")
    (status / "known-issues.md").write_text("none")
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

    # Mutable renderer output so the test can flip the payload and
    # observe a delta fire.
    state = {
        "running_now": {
            "markdown": "initial",
            "rows": [],
        }
    }

    def running_now():
        return dict(state["running_now"])

    ag_ui = AgUiBroadcaster(
        {"running_now": running_now},
        tick_seconds=0.05,
    )
    cfg.ag_ui = ag_ui
    ag_ui.start()

    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield cfg, srv.server_address[1], state
    finally:
        ag_ui.stop.set()
        srv.shutdown()
        srv.server_close()


# ─── helpers ─────────────────────────────────────────────────────────


def _open(port: int, query: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/events?{query}")
    resp = conn.getresponse()
    resp.fp.raw._sock.settimeout(3.0)
    return conn, resp


def _read_data_frame(resp: http.client.HTTPResponse, *, timeout: float = 3.0):
    """Read frames until we see a JSON data event, skipping heartbeats
    + the opening ``: alice-ag-ui v1`` banner comment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = _read_frame(resp, timeout=max(0.1, deadline - time.monotonic()))
        if ev == "heartbeat":
            continue
        return ev
    return None


def _read_frame(resp: http.client.HTTPResponse, *, timeout: float = 3.0):
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
            if all(ln.startswith(":") for ln in lines if ln):
                return "heartbeat"
            data_parts = [
                ln[len("data:"):].lstrip(" ")
                for ln in lines
                if ln.startswith("data:")
            ]
            if not data_parts:
                continue
            try:
                return json.loads("\n".join(data_parts))
            except ValueError:
                return None
    return None


# ─── /events contract ────────────────────────────────────────────────


def test_events_requires_key(site):
    _, port, _ = site
    conn, resp = _open(port, "section=running_now")
    try:
        assert resp.status == 403
    finally:
        resp.close()
        conn.close()


def test_events_rejects_unknown_section(site):
    _, port, _ = site
    conn, resp = _open(port, f"section=waiting_on_you&k={KEY}")
    try:
        assert resp.status == 404
    finally:
        resp.close()
        conn.close()


def test_events_requires_section_param(site):
    _, port, _ = site
    conn, resp = _open(port, f"k={KEY}")
    try:
        assert resp.status == 404
    finally:
        resp.close()
        conn.close()


def test_snapshot_first_shape(site):
    """The first event on every connection is a STATE_SNAPSHOT keyed
    by section name — per the AG-UI spec + connectivity-guide pin."""
    _, port, _ = site
    conn, resp = _open(port, f"section=running_now&k={KEY}")
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith(
            "text/event-stream"
        )
        ev = _read_data_frame(resp)
        assert isinstance(ev, dict), f"expected snapshot dict, got {ev!r}"
        assert ev["type"] == "STATE_SNAPSHOT"
        assert "running_now" in ev["snapshot"]
        assert ev["snapshot"]["running_now"]["markdown"] == "initial"
        # Private ``_section`` routing label must not leak over the wire.
        assert "_section" not in ev
    finally:
        resp.close()
        conn.close()


def test_delta_fires_on_change(site):
    """Flipping the running_now payload produces another
    STATE_SNAPSHOT on the stream (snapshot-first means lists get
    replaced, so every change is a full snapshot — not a patch)."""
    _, port, state = site
    conn, resp = _open(port, f"section=running_now&k={KEY}")
    try:
        first = _read_data_frame(resp)
        assert isinstance(first, dict)
        assert first["type"] == "STATE_SNAPSHOT"

        # Flip the renderer's output; the next tick should fire a new
        # snapshot event into the subscriber mailbox.
        state["running_now"] = {
            "markdown": "updated",
            "rows": [
                {
                    "branch": "feat/x",
                    "worktree": "/w/x",
                    "alive": True,
                    "pr_url": "",
                    "elapsed": "1m",
                    "task_status": "building",
                    "issue": "639",
                }
            ],
        }

        # Skip heartbeats until we see the next data event.
        for _ in range(100):
            ev = _read_frame(resp, timeout=2.0)
            if ev == "heartbeat":
                continue
            assert isinstance(ev, dict), f"expected dict, got {ev!r}"
            assert ev["type"] == "STATE_SNAPSHOT"
            assert ev["snapshot"]["running_now"]["markdown"] == "updated"
            assert len(ev["snapshot"]["running_now"]["rows"]) == 1
            return
        pytest.fail("no update event arrived within polling budget")
    finally:
        resp.close()
        conn.close()


def test_heartbeat_fires_when_idle(site):
    """With a 0.1s heartbeat the pump emits at least one `: ping`
    comment during a short idle window."""
    _, port, _ = site
    conn, resp = _open(port, f"section=running_now&k={KEY}")
    try:
        first = _read_data_frame(resp)
        assert isinstance(first, dict)
        saw_heartbeat = False
        # Budget: enough ticks to see one heartbeat even if the first
        # frame consumed a few hundred ms.
        for _ in range(20):
            ev = _read_frame(resp, timeout=1.0)
            if ev == "heartbeat":
                saw_heartbeat = True
                break
            # A spurious snapshot (payload didn't actually change) is
            # fine — just keep reading.
            if ev is None:
                pytest.fail("stream died before heartbeat")
        assert saw_heartbeat
    finally:
        resp.close()
        conn.close()


def test_events_head_probe_is_ok(site):
    """``HEAD /events?section=running_now&k=…`` returns 200 + the
    SSE content-type without pinning a long connection."""
    _, port, _ = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("HEAD", f"/events?section=running_now&k={KEY}")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith(
            "text/event-stream"
        )
    finally:
        resp.close()
        conn.close()


# ─── /ui/ bundle serving ─────────────────────────────────────────────


def test_ui_running_now_serves_react_index(site):
    """``GET /ui/running-now`` returns the Vite bundle's index.html."""
    _, port, _ = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/ui/running-now")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        body = resp.read().decode("utf-8")
        ctype = resp.getheader("Content-Type", "")
        assert ctype.startswith("text/html")
        # Vite emits a ``<div id="root"></div>`` that the bundle mounts
        # into on load. If that's missing the bundle is broken.
        assert 'id="root"' in body
        # Bundle entry chunk is loaded from ``/ui/assets/`` so the
        # importmap-free pattern + `base: '/ui/'` in vite.config.ts
        # are both working.
        assert "/ui/assets/" in body
    finally:
        resp.close()
        conn.close()


def test_ui_root_also_serves_index(site):
    """``GET /ui/`` (bare) also serves index.html — same SPA shell."""
    _, port, _ = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/ui/")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        body = resp.read().decode("utf-8")
        assert 'id="root"' in body
    finally:
        resp.close()
        conn.close()


def test_ui_assets_blocks_path_traversal(site):
    """A ``../`` escape attempt must not read files outside
    ``react-dist/``."""
    _, port, _ = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # urllib's path quoting would normalize this; send a literal
    # traversal string on the raw HTTP line instead.
    conn.request("GET", "/ui/assets/../../server.py")
    resp = conn.getresponse()
    try:
        # BaseHTTPRequestHandler normalises ``..`` in self.path, so
        # the handler sees ``/server.py`` — that falls through to the
        # static-file resolver which 404s. Either way: no 200 for
        # server.py.
        assert resp.status == 404
    finally:
        resp.close()
        conn.close()


def test_ui_assets_rejects_unknown_suffix(site):
    """Even files that exist inside react-dist/ must 404 when the
    suffix isn't on the allowlist — stops stray dotfiles from leaking."""
    _, port, _ = site
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/ui/assets/notes.secret")
    resp = conn.getresponse()
    try:
        assert resp.status == 404
    finally:
        resp.close()
        conn.close()


# ─── encoder unit tests ──────────────────────────────────────────────


def test_encode_event_strips_private_section_label():
    """``_section`` is an internal routing key, not part of the AG-UI
    wire format — encode_event must strip it."""
    frame = encode_event(
        {
            "type": "STATE_SNAPSHOT",
            "snapshot": {"running_now": {"markdown": "m", "rows": []}},
            "ts": "2026-11-17T00:00:00Z",
            "_section": "running_now",
        }
    )
    assert frame.startswith(b"data: ")
    assert frame.endswith(b"\n\n")
    payload = json.loads(frame[len("data: "):].strip())
    assert payload["type"] == "STATE_SNAPSHOT"
    assert "_section" not in payload


def test_broadcaster_snapshot_shape():
    """``broadcaster.snapshot(section)`` produces a STATE_SNAPSHOT
    whose ``snapshot`` object is keyed by section name."""
    def render() -> dict:
        return {"markdown": "hi", "rows": []}

    bc = AgUiBroadcaster({"running_now": render}, tick_seconds=60)
    ev = bc.snapshot("running_now")
    assert ev["type"] == "STATE_SNAPSHOT"
    assert ev["snapshot"]["running_now"]["markdown"] == "hi"


def test_broadcaster_tick_emits_on_change():
    """Flipping the renderer output produces exactly one tick event."""
    state = {"v": 1}

    def render() -> dict:
        return {"markdown": str(state["v"]), "rows": []}

    bc = AgUiBroadcaster({"running_now": render}, tick_seconds=60)
    # Prime the cache so the first tick doesn't emit a baseline.
    bc.snapshot("running_now")
    assert bc.tick() == []  # idempotent on no-change
    state["v"] = 2
    evs = bc.tick()
    assert len(evs) == 1
    assert evs[0]["snapshot"]["running_now"]["markdown"] == "2"


def test_broadcaster_render_failure_is_fenced():
    """A renderer that raises must not kill the broadcast — its
    section just returns a '(render failed: …)' marker."""
    def render() -> dict:
        raise RuntimeError("boom")

    bc = AgUiBroadcaster({"running_now": render}, tick_seconds=60)
    ev = bc.snapshot("running_now")
    section = ev["snapshot"]["running_now"]
    assert "render failed" in section["markdown"]


def test_check_key_constant_time_compare():
    """Trivial but pinned: wrong keys and empty keys reject."""
    assert check_key("abc", "abc") is True
    assert check_key("abc", "abd") is False
    assert check_key("abc", "") is False
    assert check_key("", "abc") is False
