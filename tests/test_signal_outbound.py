"""Outbound-awareness: capture proxy, JSONL reader, formatter, dispatch hook."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from alice_speaking import _dispatch, signal_outbound as so
from alice_speaking.infra.signal_outbound_proxy import build_app

JASON = "+14357091512"
KATIE = "+15857544003"
UTC = dt.timezone.utc


def _send(rid, msg, to=JASON):
    return {
        "jsonrpc": "2.0",
        "method": "send",
        "id": rid,
        "params": {"account": "+15853109661", "message": msg, "recipients": [to]},
    }


# -- capture ---------------------------------------------------------------


def test_records_from_rpc_classifies_sources():
    ok = {"jsonrpc": "2.0", "id": "x", "result": {"timestamp": 1}}
    ha = so.records_from_rpc(
        _send("ha-alert", "Door open"), dict(ok, id="ha-alert"), client="10.20.30.228"
    )
    assert ha[0]["source"] == "apartment-home-assistant"
    assert (
        ha[0]["ok"]
        and ha[0]["recipients"] == [JASON]
        and ha[0]["message"] == "Door open"
    )
    assert so.records_from_rpc(_send("notify", "hi"), None)[0]["source"] == "notify-cli"
    assert (
        so.records_from_rpc(_send("speaking-send-1", "hi"), None)[0]["source"]
        == "speaking"
    )
    assert so.records_from_rpc(_send("send-2", "hi"), None)[0]["source"] == "speaking"
    assert (
        so.records_from_rpc(_send("x1", "hi"), None, client="10.20.30.229")[0]["source"]
        == "home-assistant"
    )
    assert (
        so.records_from_rpc(_send("job", "hi"), None, client="1.2.3.4")[0]["source"]
        == "rpc:job@1.2.3.4"
    )


def test_records_skip_non_send_and_mark_errors():
    body = [
        {"jsonrpc": "2.0", "method": "sendTyping", "id": "t", "params": {}},
        _send("a", "one"),
        _send("b", "two"),
    ]
    resp = [
        {"id": "a", "result": {}},
        {"id": "b", "error": {"code": -1, "message": "boom"}},
    ]
    recs = so.records_from_rpc(body, resp)
    assert [r["message"] for r in recs] == ["one", "two"]
    assert [r["ok"] for r in recs] == [True, False]


def test_append_rotates(tmp_path, monkeypatch):
    p = tmp_path / "out.jsonl"
    monkeypatch.setattr(so, "ROTATE_BYTES", 10)
    so.append_records([{"a": 1}, {"a": 2}], p)
    so.append_records([{"a": 3}], p)
    assert (tmp_path / "out.jsonl.1").exists()
    assert [json.loads(ln)["a"] for ln in p.read_text().splitlines()] == [3]


# -- proxy -----------------------------------------------------------------


class _StreamingTransport(httpx.AsyncBaseTransport):
    """Like MockTransport but leaves the response body unread, as a real
    network transport does, so the proxy's streaming path is exercised."""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request):
        await request.aread()
        r = self._handler(request)
        return httpx.Response(r.status_code, headers=r.headers, stream=_Body(r.content))


class _Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        yield self._data


def _upstream(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/v1/rpc":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"timestamp": 1},
                },
            )
        return httpx.Response(200, text="pong")

    return _StreamingTransport(handler)


def test_proxy_forwards_and_captures_sends(tmp_path):
    seen = []
    log = tmp_path / "out.jsonl"
    app = build_app("http://upstream", log, transport=_upstream(seen))
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/rpc",
            json=_send("ha-alert", "Apartment door has been open for 5 minutes."),
        )
        assert r.status_code == 200 and r.json()["result"] == {"timestamp": 1}
        r = c.post(
            "/api/v1/rpc", json={"jsonrpc": "2.0", "method": "version", "id": "ping"}
        )
        assert r.status_code == 200
        r = c.get("/api/v1/check")
        assert r.text == "pong"
    # Everything reached upstream byte-for-byte.
    assert [s[1] for s in seen] == ["/api/v1/rpc", "/api/v1/rpc", "/api/v1/check"]
    assert json.loads(seen[0][2])["params"]["message"].startswith("Apartment door")
    # Only the send was captured.
    recs = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert (
        len(recs) == 1
        and recs[0]["source"] == "apartment-home-assistant"
        and recs[0]["ok"]
    )


def test_proxy_returns_502_when_upstream_down(tmp_path):
    def boom(request):
        raise httpx.ConnectError("refused")

    app = build_app(
        "http://upstream", tmp_path / "o.jsonl", transport=httpx.MockTransport(boom)
    )
    with TestClient(app) as c:
        assert c.post("/api/v1/rpc", json=_send("notify", "x")).status_code == 502
    rec = json.loads((tmp_path / "o.jsonl").read_text())
    assert rec["ok"] is False


def test_proxy_capture_failure_does_not_break_send(tmp_path):
    # Log path is a directory → append fails; the send must still succeed.
    bad = tmp_path / "dir"
    bad.mkdir()
    app = build_app("http://upstream", bad, transport=_upstream([]))
    with TestClient(app) as c:
        assert c.post("/api/v1/rpc", json=_send("notify", "x")).status_code == 200


# -- reader / formatter ----------------------------------------------------


def _write(p: pathlib.Path, *recs):
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))


def _rec(ts, msg, *, source="apartment-home-assistant", to=JASON, ok=True):
    return {
        "ts": ts.isoformat(),
        "source": source,
        "recipients": [to],
        "message": msg,
        "ok": ok,
    }


def test_render_filters_and_formats(tmp_path):
    now = dt.datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    p = tmp_path / "o.jsonl"
    _write(
        p,
        _rec(now - dt.timedelta(hours=7), "too old"),
        _rec(
            now - dt.timedelta(minutes=50),
            "Apartment door has been open for 5 minutes.",
        ),
        _rec(now - dt.timedelta(minutes=40), "my own reply", source="speaking"),
        _rec(now - dt.timedelta(minutes=30), "to katie", to=KATIE),
        _rec(now - dt.timedelta(minutes=20), "failed send", ok=False),
        _rec(now - dt.timedelta(minutes=5), "Backup finished", source="notify-cli"),
    )
    block = so.render_awareness_block(JASON, path=p, now=now)
    lines = block.splitlines()
    assert lines[0].startswith("[Outbound from your Signal number NOT sent by you")
    assert len(lines) == 3
    assert (
        '(50m ago) via apartment-home-assistant: "Apartment door has been open for 5 minutes."'
        in lines[1]
    )
    assert '(5m ago) via notify-cli: "Backup finished"' in lines[2]
    assert so.render_awareness_block(KATIE, path=p, now=now).count("\n") == 1
    assert so.render_awareness_block("+10000000000", path=p, now=now) == ""
    assert so.render_awareness_block(JASON, path=tmp_path / "missing", now=now) == ""


def test_render_caps_entries_and_size(tmp_path):
    now = dt.datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    p = tmp_path / "o.jsonl"
    _write(
        p,
        *[
            _rec(now - dt.timedelta(minutes=60 - i), f"msg {i} " + "x" * 400)
            for i in range(20)
        ],
    )
    block = so.render_awareness_block(JASON, path=p, now=now)
    assert len(block) <= so.DEFAULT_MAX_CHARS
    # Newest kept, oldest dropped.
    assert '"msg 19 ' in block and '"msg 0 ' not in block
    assert block.count("\n- ") <= so.DEFAULT_MAX_ENTRIES


def test_reader_tolerates_garbage(tmp_path):
    now = dt.datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    p = tmp_path / "o.jsonl"
    p.write_text("not json\n[1,2]\n" + json.dumps(_rec(now, "ok")) + '\n{"partial')
    assert len(so.recent_external(JASON, path=p, now=now)) == 1


# -- dispatch hook / no-wake ----------------------------------------------


class _Events:
    def __init__(self):
        self.emitted = []

    def emit(self, name, **kw):
        self.emitted.append((name, kw))


def test_dispatch_prepends_block(tmp_path, monkeypatch):
    p = tmp_path / "o.jsonl"
    now = dt.datetime.now(UTC)
    _write(
        p,
        _rec(
            now - dt.timedelta(minutes=3), "Apartment door has been open for 5 minutes."
        ),
    )
    monkeypatch.setattr(so, "DEFAULT_LOG_PATH", p)
    monkeypatch.setattr(
        so,
        "render_awareness_block",
        lambda r, _orig=so.render_awareness_block, **kw: _orig(r, path=p, **kw),
    )
    ctx = SimpleNamespace(cfg=SimpleNamespace(speaking={}), events=_Events())
    out = _dispatch._with_outbound_awareness(ctx, "PROMPT", JASON, "t1")
    assert out.endswith("\n\nPROMPT") and "Apartment door" in out
    assert ctx.events.emitted[0][0] == "outbound_awareness_injected"
    # Disabled → untouched; other principal → untouched.
    ctx.cfg.speaking = {"outbound_awareness": {"enabled": False}}
    assert _dispatch._with_outbound_awareness(ctx, "PROMPT", JASON, "t2") == "PROMPT"
    ctx.cfg.speaking = {}
    assert _dispatch._with_outbound_awareness(ctx, "PROMPT", KATIE, "t3") == "PROMPT"


def test_dispatch_hook_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(so, "render_awareness_block", boom)
    ctx = SimpleNamespace(cfg=SimpleNamespace(speaking={}), events=_Events())
    assert _dispatch._with_outbound_awareness(ctx, "P", JASON, "t") == "P"


def test_capture_does_not_wake_speaking(tmp_path):
    """Capture is a plain append to one JSONL: it creates no surface,
    note, or event that any Speaking transport watches, and the only
    reader is the per-turn hook inside handle_signal."""
    import inspect

    mind = tmp_path
    log = mind / "out.jsonl"
    app = build_app("http://upstream", log, transport=_upstream([]))
    with TestClient(app) as c:
        c.post("/api/v1/rpc", json=_send("ha-alert", "door"))
    assert sorted(x.name for x in mind.iterdir()) == ["out.jsonl"]
    src = inspect.getsource(so) + inspect.getsource(
        __import__("alice_speaking.infra.signal_outbound_proxy", fromlist=["x"])
    )
    for forbidden in ("inner/surface", "inner/notes", "reload-requested", "emit("):
        assert forbidden not in src
    # The only call site is the inbound Signal turn handler.
    assert (
        "_with_outbound_awareness(ctx, prompt, source, turn_id)"
        in inspect.getsource(_dispatch.handle_signal)
    )
