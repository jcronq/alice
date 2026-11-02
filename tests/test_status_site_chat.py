"""Tests for the general Chat tab: ``/api/chat`` endpoints + the ``status-chat`` CLI."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from status_site import cli
from status_site.server import Config, make_server

KEY = "chat-key-xyz"


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


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    c.request(method, path, body=data, headers=headers or {})
    r = c.getresponse()
    out = r.status, r.read(), dict(r.getheaders())
    c.close()
    return out


def post(port, payload):
    status, body, _ = req(
        port, "POST", "/api/chat", payload, {"Content-Type": "application/json"}
    )
    return status, json.loads(body or b"{}")


def surfaces(cfg):
    return (
        sorted(cfg.surface_dir.glob("*.md")) if cfg.surface_dir.exists() else []
    )


# ---------------------------------------------------------------- auth


def test_chat_requires_key(site):
    cfg, port = site
    assert post(port, {"text": "hi"})[0] == 403
    assert post(port, {"key": "wrong", "text": "hi"})[0] == 403
    assert post(port, {"key": KEY[:-1], "text": "hi"})[0] == 403
    assert surfaces(cfg) == []
    assert not cfg.chat_log.exists()


def test_chat_rejects_empty(site):
    _, port = site
    assert post(port, {"key": KEY, "text": ""})[0] == 400
    assert post(port, {"key": KEY, "text": "   "})[0] == 400
    assert post(port, {"key": KEY})[0] == 400


# ---------------------------------------------------------------- POST happy path


def test_chat_appends_log_and_writes_surface(site):
    cfg, port = site
    status, j = post(port, {"key": KEY, "text": "hi alice"})
    assert status == 200 and j == {"ok": True}

    # Chat log has a jason-authored entry.
    lines = cfg.chat_log.read_text().splitlines()
    assert len(lines) == 1
    msg = json.loads(lines[0])
    assert msg["from"] == "jason" and msg["text"] == "hi alice" and msg["at"]

    # One surface file was written with the right frontmatter + body.
    files = surfaces(cfg)
    assert len(files) == 1
    doc = files[0].read_text()
    assert "status-chat" in files[0].name
    # Frontmatter contains the required fields (no thread/item).
    assert "type: status-chat" in doc
    assert "priority: action" in doc
    assert "reply_expected: true" in doc
    assert "source: status-site" in doc
    assert "page: chat" in doc
    assert "created:" in doc
    assert "thread:" not in doc
    assert "Item:" not in doc
    # The message itself ended up in the body.
    assert "hi alice" in doc


def test_chat_surface_includes_tail_context(site):
    cfg, port = site
    for n in range(1, 8):
        assert post(port, {"key": KEY, "text": f"msg{n}"})[0] == 200
    # Find msg7's surface by content; same-second collision names make lex
    # ordering unreliable, and mtime resolution may tie.
    latest = next(
        p.read_text() for p in surfaces(cfg) if "His new message:\nmsg7" in p.read_text()
    )
    assert "msg6" in latest and "msg2" in latest  # tail of 5 before the current
    assert "msg1" not in latest  # trimmed to CHAT_SURFACE_TAIL=5
    # 7 messages => 7 surface files were emitted.
    assert len(surfaces(cfg)) == 7


def test_chat_text_truncated(site):
    cfg, port = site
    assert post(port, {"key": KEY, "text": "x" * 9000})[0] == 200
    msg = json.loads(cfg.chat_log.read_text().splitlines()[-1])
    assert len(msg["text"]) == 8000  # MAX_TEXT


# ---------------------------------------------------------------- GET /api/chat


def test_get_chat_returns_messages(site):
    cfg, port = site
    assert post(port, {"key": KEY, "text": "one"})[0] == 200
    assert post(port, {"key": KEY, "text": "two"})[0] == 200
    status, body, h = req(port, "GET", "/api/chat?123")
    assert status == 200 and h["Content-Type"] == "application/json"
    d = json.loads(body)
    assert [m["text"] for m in d["messages"]] == ["one", "two"]
    assert all(m["from"] == "jason" for m in d["messages"])


def test_get_chat_empty_when_no_log(site):
    _, port = site
    status, body, _ = req(port, "GET", "/api/chat")
    assert status == 200
    assert json.loads(body) == {"messages": []}


def test_get_chat_includes_alice_replies(site):
    cfg, port = site
    assert post(port, {"key": KEY, "text": "hello"})[0] == 200
    # Simulate Alice replying through the CLI.
    with open(cfg.chat_log, "a") as f:
        f.write(json.dumps({"from": "alice", "at": "2026-11-02T10:00:00", "text": "hey"}) + "\n")
    _, body, _ = req(port, "GET", "/api/chat")
    d = json.loads(body)
    assert [m["from"] for m in d["messages"]] == ["jason", "alice"]


# ---------------------------------------------------------------- /api/chat is write-protected


def test_chat_log_is_not_served_as_content(site):
    cfg, port = site
    assert post(port, {"key": KEY, "text": "secret"})[0] == 200
    # Dotfile prefix means the generic content handler won't serve it.
    assert req(port, "GET", "/.chat.jsonl")[0] == 404
    assert req(port, "GET", "/chat.jsonl")[0] == 404


# ---------------------------------------------------------------- CLI


def test_status_chat_cli_appends_as_alice(tmp_path, monkeypatch):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    assert cli.main_chat(["  on it  "]) == 0
    msg = json.loads((tmp_path / ".chat.jsonl").read_text().splitlines()[-1])
    assert msg["from"] == "alice" and msg["text"] == "on it" and msg["at"]
    # Empty / wrong arity rejected.
    assert cli.main_chat(["   "]) == 2
    assert cli.main_chat([]) == 2
    assert cli.main_chat(["a", "b"]) == 2
