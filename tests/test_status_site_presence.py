"""Tests for the presence endpoints (slice-presence-aware-notifs).

Covers:
- ``PresenceState`` unit behaviour (window boundary, stamp update
  only on ``focused=true``, snapshot shape when never heartbeated).
- POST ``/api/presence`` — shared-key gate, happy path, bad body,
  bad focused.
- GET ``/api/presence/status`` — empty, fresh, stale.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from status_site.server import (
    PRESENCE_WINDOW_S,
    Config,
    PresenceState,
    make_server,
)

KEY = "presence-key-xyz"


# ─── unit: PresenceState ───────────────────────────────────────────────


def test_presence_state_snapshot_empty_default_minimal():
    p = PresenceState()
    snap = p.snapshot()
    assert snap == {"focused": False, "window_s": PRESENCE_WINDOW_S}


def test_presence_state_snapshot_empty_detailed_includes_within_s():
    p = PresenceState()
    snap = p.snapshot(detailed=True)
    assert snap == {
        "focused": False,
        "focused_within_s": None,
        "window_s": PRESENCE_WINDOW_S,
    }


def test_presence_state_snapshot_shape_omits_last_presence_at():
    # Review finding #2 (round 1): ``last_presence_at`` would be
    # laptop-activity telemetry on an unauth endpoint. Never leak it
    # even with ``detailed=True``.
    p = PresenceState()
    p.record(focused=True, now=1000.0)
    for snap in (p.snapshot(now=1000.5), p.snapshot(now=1000.5, detailed=True)):
        assert "last_presence_at" not in snap


def test_presence_state_minimal_snapshot_omits_focused_within_s():
    # Review finding #5 (max pass): seconds-since-blur is still
    # activity info; keep it behind the shared-key gate.
    p = PresenceState()
    p.record(focused=True, now=1000.0)
    snap = p.snapshot(now=1000.5)
    assert "focused_within_s" not in snap
    assert snap["focused"] is True


def test_presence_state_detailed_snapshot_has_focused_within_s():
    p = PresenceState()
    p.record(focused=True, now=1000.0)
    snap = p.snapshot(now=1001.5, detailed=True)
    assert snap["focused"] is True
    assert snap["focused_within_s"] == 1.5


def test_presence_state_records_focused_false_clears_stamp():
    # Review finding #1 (round 1): ``focused=false`` is the blur
    # signal from the browser. The server MUST invalidate the stamp
    # — otherwise a tab the user just closed still looks focused
    # until the 45 s window ages out.
    p = PresenceState()
    p.record(focused=True, now=1000.0)
    p.record(focused=False, now=1001.0)
    snap = p.snapshot(now=1002.0, detailed=True)
    assert snap["focused"] is False
    assert snap["focused_within_s"] is None


def test_presence_state_window_boundary():
    p = PresenceState()
    p.record(focused=True, now=1000.0)
    inside = p.snapshot(now=1000.0 + PRESENCE_WINDOW_S - 0.001)
    outside = p.snapshot(now=1000.0 + PRESENCE_WINDOW_S + 0.001)
    assert inside["focused"] is True
    assert outside["focused"] is False


def test_presence_state_negative_delta_clamped():
    # Clock skew between the record() and snapshot() calls shouldn't
    # produce a negative focused_within_s — treat as 0.
    p = PresenceState()
    p.record(focused=True, now=1000.0)
    snap = p.snapshot(now=999.0, detailed=True)
    assert snap["focused_within_s"] == 0.0
    assert snap["focused"] is True


# ─── integration: HTTP endpoints ───────────────────────────────────────


@pytest.fixture
def site(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text("# Overview\n")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
        upload_dir_alias="/home/alice/alice-mind/inner/status-uploads",
        max_upload=1024,
    )
    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield cfg, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    c.request(method, path, body=data, headers=headers or {})
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


def test_get_status_empty(site):
    _, port = site
    status, body = _req(port, "GET", "/api/presence/status")
    assert status == 200
    data = json.loads(body)
    assert data["focused"] is False
    assert data["window_s"] == PRESENCE_WINDOW_S
    # No laptop-activity telemetry on the unauth endpoint.
    assert "last_presence_at" not in data
    assert "focused_within_s" not in data


def test_get_status_with_key_unlocks_focused_within_s(site):
    cfg, port = site
    # Record a heartbeat so focused_within_s is non-null.
    cfg.presence.record(focused=True)
    status, body = _req(port, "GET", f"/api/presence/status?k={KEY}")
    assert status == 200
    data = json.loads(body)
    assert data["focused"] is True
    assert "focused_within_s" in data
    assert isinstance(data["focused_within_s"], (int, float))


def test_get_status_wrong_key_falls_back_to_minimal(site):
    cfg, port = site
    cfg.presence.record(focused=True)
    status, body = _req(port, "GET", "/api/presence/status?k=wrong")
    assert status == 200
    data = json.loads(body)
    assert data["focused"] is True
    # Wrong key: no secondary field. Still 200 — the minimal shape
    # is the always-available LAN read.
    assert "focused_within_s" not in data


def test_post_presence_happy_path(site):
    cfg, port = site
    status, body = _req(
        port, "POST", "/api/presence", body={"key": KEY, "focused": True}
    )
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["focused"] is True
    # POST returns the detailed snapshot — caller already proved
    # knowledge of the key via the body; no new exposure.
    assert data["focused_within_s"] is not None
    assert data["focused_within_s"] < 1.0
    # And the GET sees the same state (minimal shape).
    status2, body2 = _req(port, "GET", "/api/presence/status")
    assert status2 == 200
    data2 = json.loads(body2)
    assert data2["focused"] is True
    assert "focused_within_s" not in data2


def test_post_presence_rejects_bad_key(site):
    _, port = site
    status, body = _req(
        port, "POST", "/api/presence", body={"key": "wrong", "focused": True}
    )
    assert status == 403
    assert b"bad key" in body


def test_post_presence_rejects_bad_focused(site):
    _, port = site
    status, body = _req(
        port, "POST", "/api/presence", body={"key": KEY, "focused": "yes"}
    )
    assert status == 400
    assert b"bad focused" in body


def test_post_presence_false_clears_stamp(site):
    cfg, port = site
    # Focus, then blur.
    _req(port, "POST", "/api/presence", body={"key": KEY, "focused": True})
    _req(port, "POST", "/api/presence", body={"key": KEY, "focused": False})
    status, body = _req(port, "GET", f"/api/presence/status?k={KEY}")
    data = json.loads(body)
    assert data["focused"] is False
    assert data["focused_within_s"] is None


def test_post_presence_rejects_malformed_json(site):
    _, port = site
    status, body = _req(
        port,
        "POST",
        "/api/presence",
        body=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400


def test_get_status_no_auth_required(site):
    # The GET is deliberately unauthenticated — status-site is LAN-
    # only and the shape carries no PII. A missing / wrong key must
    # not 403.
    _, port = site
    status, _ = _req(port, "GET", "/api/presence/status")
    assert status == 200
    status, _ = _req(port, "GET", "/api/presence/status?k=wrong")
    assert status == 200


def test_presence_state_ages_out(site):
    cfg, port = site
    # Manually jam a stale stamp.
    cfg.presence.record(focused=True, now=time.time() - PRESENCE_WINDOW_S - 5)
    status, body = _req(port, "GET", f"/api/presence/status?k={KEY}")
    data = json.loads(body)
    assert data["focused"] is False
    assert data["focused_within_s"] is not None
    assert data["focused_within_s"] > PRESENCE_WINDOW_S
