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


# ---------------------------------------------------------------- A2UI emitter (#651)


def test_chat_a2ui_emitter_shape(site):
    """``sources.chat.render`` emits the three-envelope A2UI surface
    (createSurface → updateComponents → updateDataModel) the Chat page
    reduces into.
    """
    from status_site.sources import chat as chat_page

    cfg, port = site
    # Seed one message so the data-model envelope has real content.
    assert post(port, {"key": KEY, "text": "hi alice"})[0] == 200

    payload = chat_page.render(cfg)
    assert "a2ui" in payload
    envelopes = payload["a2ui"]
    assert [e["type"] for e in envelopes] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]
    assert all(e["surfaceId"] == "chat" for e in envelopes)

    # updateComponents includes a root, a thread list bound to
    # ``/messages``, a textarea, and a send button whose action the
    # client switches on.
    comps = {c["id"]: c for c in envelopes[1]["components"]}
    assert comps["root"]["type"] == "list"
    assert comps["thread"]["type"] == "list"
    assert comps["thread"]["props"]["boundTo"] == "/messages"
    assert comps["composer"]["type"] == "textarea"
    assert comps["send"]["type"] == "button"
    assert comps["send"]["props"]["action"] == "chat.send"
    assert comps["send"]["props"]["textareaId"] == "composer"

    # The final updateDataModel carries the chat log at ``/messages``.
    data_env = envelopes[2]
    assert data_env["path"] == "/messages"
    assert [m["text"] for m in data_env["value"]] == ["hi alice"]


def test_chat_a2ui_empty_log_yields_empty_messages(site):
    """No chat log yet → the data-model carries an empty list, not None."""
    from status_site.sources import chat as chat_page

    cfg, _ = site
    payload = chat_page.render(cfg)
    assert payload["a2ui"][-1]["value"] == []


def test_chat_post_with_image_then_get_returns_attachment(site):
    """Integration (#655): POST an image, file lands under chat/<date>/, GET shows it."""
    import base64
    import time

    cfg, port = site
    status, j = post(
        port,
        {
            "key": KEY,
            "text": "",
            "files": [
                {"name": "pasted-20260101T000000.png", "data": base64.b64encode(b"PNG").decode()}
            ],
        },
    )
    assert status == 200 and j == {"ok": True}
    stored = list((cfg.upload_dir / "chat" / time.strftime("%Y-%m-%d")).glob("*pasted-*.png"))
    assert len(stored) == 1 and stored[0].read_bytes() == b"PNG"

    status, body, _ = req(port, "GET", "/api/chat")
    assert status == 200
    (msg,) = json.loads(body)["messages"]
    assert msg["files"] == ["pasted-20260101T000000.png"]
    assert msg["attachments"][0]["path"].endswith(stored[0].name)


def test_chat_post_rejects_disallowed_attachment_type(site):
    import base64

    _, port = site
    status, _ = post(
        port,
        {"key": KEY, "text": "x", "files": [{"name": "a.exe", "data": base64.b64encode(b"x").decode()}]},
    )
    assert status == 415


def test_chat_composer_declares_attachments_and_submit(site):
    from status_site.sources import chat as chat_page

    cfg, _ = site
    comps = {c["id"]: c for c in chat_page.render(cfg)["a2ui"][1]["components"]}
    assert comps["composer"]["props"]["attachments"] is True
    assert comps["composer"]["props"]["submitId"] == "send"
    assert comps[comps["composer"]["props"]["submitId"]]["props"]["action"] == "chat.send"


def test_chat_ux_js_is_served_and_wired(site):
    status, body, headers = req(site[1], "GET", "/chat-ux.js")
    assert status == 200 and b"shouldSubmitOnKey" in body
    assert "javascript" in headers.get("Content-Type", "")
    status, html, _ = req(site[1], "GET", "/index.html")
    html = html.decode()
    assert 'from "./chat-ux.js"' in html
    for needle in ("onKeyDown", "onPaste", "ChatThread", "chat-file-input"):
        assert needle in html


# ---------------------------------------------------------------- pagination (#671)


def _seed_chat(cfg, n):
    """n messages, one minute apart, ``m000`` oldest."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 11, 6, 0, 0, 0, tzinfo=timezone.utc)
    cfg.chat_log.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.chat_log, "w") as f:
        for i in range(n):
            f.write(json.dumps({
                "from": "jason" if i % 2 == 0 else "alice",
                "at": (t0 + timedelta(minutes=i)).isoformat(timespec="seconds"),
                "text": f"m{i:03d}",
            }) + "\n")


def test_read_chat_before_returns_older_chronological_page(site):
    from status_site.server import read_chat

    cfg, _ = site
    _seed_chat(cfg, 100)
    cutoff = read_chat(cfg, limit=0)[60]["at"]
    page = read_chat(cfg, limit=10, before=cutoff)
    assert [m["text"] for m in page] == [f"m{i:03d}" for i in range(50, 60)]
    assert all(m["at"] < cutoff for m in page)


def test_read_chat_before_compares_across_timezone_offsets(site):
    from status_site.server import read_chat

    cfg, _ = site
    _seed_chat(cfg, 5)  # 00:00Z .. 00:04Z
    # 2026-11-05T19:03:00-05:00 == 2026-11-06T00:03:00Z
    page = read_chat(cfg, limit=50, before="2026-11-05T19:03:00-05:00")
    assert [m["text"] for m in page] == ["m000", "m001", "m002"]


def test_read_chat_before_invalid_timestamp_returns_empty(site):
    from status_site.server import read_chat

    cfg, _ = site
    _seed_chat(cfg, 10)
    assert read_chat(cfg, limit=5, before="not-a-timestamp") == []
    assert read_chat(cfg, limit=5, before="") == []


def test_read_chat_before_skips_rows_without_parsable_at(site):
    from status_site.server import read_chat

    cfg, _ = site
    _seed_chat(cfg, 3)
    with open(cfg.chat_log, "a") as f:
        f.write(json.dumps({"from": "alice", "text": "no at"}) + "\n")
    page = read_chat(cfg, limit=50, before="2030-01-01T00:00:00+00:00")
    assert [m["text"] for m in page] == ["m000", "m001", "m002"]


def test_get_chat_paginates_backward_through_300_messages(site):
    from urllib.parse import quote

    cfg, port = site
    _seed_chat(cfg, 300)
    _, body, _ = req(port, "GET", "/api/chat")
    tail = json.loads(body)["messages"]
    assert [m["text"] for m in tail][-1] == "m299"

    seen = [m["text"] for m in tail]
    earliest = tail[0]["at"]
    for _ in range(3):
        status, body, _ = req(
            port, "GET", f"/api/chat?before={quote(earliest)}&limit=50"
        )
        assert status == 200
        page = json.loads(body)["messages"]
        assert len(page) == 50
        assert page == sorted(page, key=lambda m: m["at"])
        seen = [m["text"] for m in page] + seen
        earliest = page[0]["at"]
    assert seen == [f"m{i:03d}" for i in range(300 - len(seen), 300)]
    assert len(seen) == len(tail) + 150


def test_get_chat_before_limit_defaults_and_caps(site):
    from urllib.parse import quote

    cfg, port = site
    _seed_chat(cfg, 300)
    far = quote("2030-01-01T00:00:00+00:00")

    def fetch(qs):
        _, body, _ = req(port, "GET", f"/api/chat?before={far}{qs}")
        return json.loads(body)["messages"]

    assert len(fetch("")) == 50
    assert len(fetch("&limit=1000")) == 200
    assert len(fetch("&limit=7")) == 7
    assert len(fetch("&limit=abc")) == 50
    assert len(fetch("&limit=0")) == 50
    assert len(fetch("&limit=-3")) == 50
    assert fetch("&limit=7")[-1]["text"] == "m299"


def test_get_chat_before_invalid_is_empty_not_tail(site):
    cfg, port = site
    _seed_chat(cfg, 10)
    _, body, _ = req(port, "GET", "/api/chat?before=garbage&limit=5")
    assert json.loads(body) == {"messages": []}


def test_chat_snapshot_carries_only_recent_tail(site):
    from status_site.server import CHAT_SNAPSHOT_LIMIT
    from status_site.sources import chat as chat_page

    cfg, _ = site
    _seed_chat(cfg, 300)
    msgs = chat_page.render(cfg)["a2ui"][-1]["value"]
    assert len(msgs) == CHAT_SNAPSHOT_LIMIT == 50
    assert msgs[0]["text"] == "m250" and msgs[-1]["text"] == "m299"


def test_chat_snapshot_drops_working_groups_older_than_window(site):
    from status_site.chat_status import STATUS_FILE
    from status_site.sources import chat as chat_page

    cfg, _ = site
    _seed_chat(cfg, 300)  # window starts at m250 = 04:10Z
    rows = [
        {"turn_id": "old", "kind": "turn_start", "at": "2026-11-06T00:05:00+00:00"},
        {"turn_id": "new", "kind": "turn_start", "at": "2026-11-06T04:55:30+00:00"},
    ]
    (cfg.status_dir / STATUS_FILE).write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    msgs = chat_page.render(cfg)["a2ui"][-1]["value"]
    groups = [m["turn_id"] for m in msgs if m.get("variant") == "working-group"]
    assert groups == ["new"]
