"""Tests for the viewer-side /chat + /api/chat/* routes.

The viewer's chat routes are thin proxies to the speaking daemon's
viewer-chat HTTP ingress. We stub :mod:`alice_viewer.chat_client` so
the route handlers exercise their own logic without bringing up a
daemon. The chat-page HTML route is smoke-tested for status + key
markers.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from alice_viewer.main import create_app
from alice_viewer.settings import Paths


@pytest.fixture
def paths(tmp_path: pathlib.Path) -> Paths:
    mind = tmp_path / "mind"
    state = tmp_path / "state"
    (mind / "inner" / "state").mkdir(parents=True)
    state.mkdir()
    return Paths(
        thinking_log=state / "thinking.log",
        speaking_log=state / "speaking.log",
        turn_log=mind / "inner" / "state" / "speaking-turns.jsonl",
        mind_dir=mind,
        state_dir=state,
    )


@pytest.fixture
def app(paths):
    return create_app(paths=paths)


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# /chat HTML view


def test_chat_view_renders_html(client):
    r = client.get("/chat")
    assert r.status_code == 200
    body = r.text
    # Core elements the JS expects to find in the DOM.
    assert 'id="chat-log"' in body
    assert 'id="chat-form"' in body
    assert 'id="chat-input"' in body
    # The nav link to /chat is marked active.
    assert 'class="on"' in body
    assert "/chat" in body


# ---------------------------------------------------------------------------
# /api/chat/history


def test_api_chat_history_proxies_to_daemon(client, monkeypatch):
    captured: dict = {}

    async def fake_history(*, channel=None, limit=100, timeout=10.0):
        captured["channel"] = channel
        captured["limit"] = limit
        return {
            "channel": channel or "viewer-chat-main",
            "messages": [{"role": "user", "text": "hi", "ts": 1.0}],
        }

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "fetch_history", fake_history)
    r = client.get("/api/chat/history")
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "viewer-chat-main"
    assert body["messages"][0]["text"] == "hi"
    assert captured["limit"] == 100


def test_api_chat_history_returns_502_on_daemon_error(client, monkeypatch):
    async def fake_history(*, channel=None, limit=100, timeout=10.0):
        raise RuntimeError("daemon said no")

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "fetch_history", fake_history)
    r = client.get("/api/chat/history")
    assert r.status_code == 502
    body = r.json()
    assert "daemon said no" in body["error"]
    assert body["kind"] == "daemon_error"


def test_api_chat_history_returns_503_on_unreachable_daemon(client, monkeypatch):
    async def fake_history(*, channel=None, limit=100, timeout=10.0):
        raise ConnectionError("connect refused")

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "fetch_history", fake_history)
    r = client.get("/api/chat/history")
    assert r.status_code == 503
    assert r.json()["kind"] == "unreachable"


# ---------------------------------------------------------------------------
# /api/chat/send — validation + proxy


def test_api_chat_send_rejects_empty_text(client):
    r = client.post("/api/chat/send", json={"text": "   "})
    assert r.status_code == 400


def test_api_chat_send_rejects_non_object_body(client):
    r = client.post(
        "/api/chat/send",
        content=b"[]",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_api_chat_send_rejects_invalid_json(client):
    r = client.post(
        "/api/chat/send",
        content=b"this is not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_api_chat_send_proxies_to_daemon(client, monkeypatch):
    captured: dict = {}

    async def fake_send(text, *, channel=None, timeout=10.0):
        captured["text"] = text
        captured["channel"] = channel
        return {"ok": True, "channel": channel or "viewer-chat-main"}

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "send_message", fake_send)
    r = client.post("/api/chat/send", json={"text": "hello"})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert captured["text"] == "hello"


def test_api_chat_send_passes_explicit_channel(client, monkeypatch):
    captured: dict = {}

    async def fake_send(text, *, channel=None, timeout=10.0):
        captured["channel"] = channel
        return {"ok": True, "channel": channel}

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "send_message", fake_send)
    r = client.post(
        "/api/chat/send", json={"text": "hi alt", "channel": "alt-conv"}
    )
    assert r.status_code == 202
    assert captured["channel"] == "alt-conv"


def test_api_chat_send_502_on_daemon_error(client, monkeypatch):
    async def fake_send(text, *, channel=None, timeout=10.0):
        raise RuntimeError("queue full")

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "send_message", fake_send)
    r = client.post("/api/chat/send", json={"text": "hello"})
    assert r.status_code == 502
    body = r.json()
    assert "queue full" in body["error"]


# ---------------------------------------------------------------------------
# /api/chat/stream — SSE happy path


def test_api_chat_stream_relays_daemon_events(client, monkeypatch):
    """SSE proxy: each event from the daemon stream becomes one
    ``message`` event in the viewer's response."""

    async def fake_stream(*, channel=None, timeout=600.0):
        for ev in [
            {"type": "ack"},
            {"type": "chunk", "text": "hello"},
            {"type": "chunk", "text": " world"},
            {"type": "done"},
        ]:
            yield ev

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "stream_events", fake_stream)
    with client.stream("GET", "/api/chat/stream") as resp:
        assert resp.status_code == 200
        # Read until we collect 4 data: lines.
        collected: list[str] = []
        for raw in resp.iter_lines():
            line = raw if isinstance(raw, str) else raw.decode("utf-8")
            if line.startswith("data:"):
                collected.append(line[len("data:") :].strip())
                if len(collected) == 4:
                    break
    assert len(collected) == 4
    payloads = [json.loads(c) for c in collected]
    assert payloads[0] == {"type": "ack"}
    assert payloads[1] == {"type": "chunk", "text": "hello"}
    assert payloads[3] == {"type": "done"}


def test_api_chat_stream_emits_error_event_on_failure(client, monkeypatch):
    async def fake_stream(*, channel=None, timeout=600.0):
        raise RuntimeError("daemon dead")
        yield  # unreachable; makes this an async generator

    from alice_viewer import chat_client

    monkeypatch.setattr(chat_client, "stream_events", fake_stream)
    with client.stream("GET", "/api/chat/stream") as resp:
        assert resp.status_code == 200
        seen_error = False
        for raw in resp.iter_lines():
            line = raw if isinstance(raw, str) else raw.decode("utf-8")
            if line.startswith("event: error"):
                seen_error = True
                break
        assert seen_error
