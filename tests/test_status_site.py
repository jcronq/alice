"""Tests for the status site server (src/status_site) and the status-reply CLI."""

from __future__ import annotations

import base64
import http.client
import json
import threading
from pathlib import Path

import pytest

from status_site import cli
from status_site.server import Config, make_server, thread_id

KEY = "test-key-123"


@pytest.fixture
def site(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text("# Overview\n\n- [ ] **Fix the thing**: details\n")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    (status / ".replies.jsonl").write_text('{"secret": true}\n')
    (status / "threads" / "existing.jsonl").write_text(
        json.dumps({"from": "jason", "at": "2026-10-27T10:00:00", "text": "first"})
        + "\n"
        + json.dumps({"from": "alice", "at": "2026-10-27T10:01:00", "text": "on it"})
        + "\n"
    )
    (tmp_path / "secret.txt").write_text("outside")
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
        port, "POST", "/api/reply", payload, {"Content-Type": "application/json"}
    )
    return status, json.loads(body or b"{}")


def surfaces(cfg):
    return (
        sorted(p for p in cfg.surface_dir.glob("*.md"))
        if cfg.surface_dir.exists()
        else []
    )


# ---------------------------------------------------------------- auth


def test_reply_requires_key(site):
    cfg, port = site
    assert post(port, {"item": "x", "text": "hi"})[0] == 403
    assert post(port, {"key": "wrong", "item": "x", "text": "hi"})[0] == 403
    assert post(port, {"key": KEY[:-1], "item": "x", "text": "hi"})[0] == 403
    assert surfaces(cfg) == []
    assert not (cfg.threads_dir / "x.jsonl").exists()


def test_bad_json_and_empty(site):
    cfg, port = site
    status, _, _ = req(
        port, "POST", "/api/reply", b"{not json", {"Content-Type": "application/json"}
    )
    assert status == 400
    assert post(port, {"key": KEY, "item": "x", "text": "   "})[0] == 400
    assert req(port, "POST", "/api/other", {"key": KEY})[0] == 404


# ---------------------------------------------------------------- thread id


def test_thread_id_derivation():
    assert thread_id(None, "PFL: confirm the payout — details") == "pfl"
    assert thread_id("", "Aimax BIOS: flash it") == "aimax-bios"
    assert thread_id("pfl-confirm-the-payout", "whatever") == "pfl-confirm-the-payout"
    assert thread_id("../../etc/passwd", "x") == "etcpasswd"
    assert thread_id("", "") == "general"


def test_thread_derived_from_item_title_when_missing(site):
    cfg, port = site
    status, j = post(
        port, {"key": KEY, "item": "Fix the Thing: details here", "text": "hello"}
    )
    assert status == 200 and j["thread"] == "fix-the-thing"
    assert (cfg.threads_dir / "fix-the-thing.jsonl").exists()


# ---------------------------------------------------------------- reply + surface


def test_reply_appends_thread_and_writes_surface_with_history(site):
    cfg, port = site
    status, j = post(
        port,
        {
            "key": KEY,
            "item": "Existing: item text",
            "thread": "existing",
            "text": "next step?",
            "page": "index",
        },
    )
    assert status == 200 and j == {"ok": True, "thread": "existing"}

    lines = (cfg.threads_dir / "existing.jsonl").read_text().splitlines()
    assert len(lines) == 3
    last = json.loads(lines[-1])
    assert (
        last["from"] == "jason" and last["text"] == "next step?" and last["files"] == []
    )

    (s,) = surfaces(cfg)
    doc = s.read_text()
    assert doc.startswith("---\n")
    front = doc.split("---\n")[1]
    fm = dict(line.split(": ", 1) for line in front.strip().splitlines())
    assert fm["type"] == "status-reply"
    assert fm["priority"] == "action"
    assert fm["reply_expected"] == "true"
    assert fm["source"] == "status-site"
    assert fm["page"] == "index"
    assert fm["thread"] == "existing"
    assert fm["detail"].startswith("status-reply-existing")
    assert "jason: first" in doc and "alice: on it" in doc
    assert "next step?" in doc
    assert 'status-reply existing "' in doc
    assert not any(p.name.startswith(".tmp") for p in cfg.surface_dir.iterdir())

    log = cfg.reply_log.read_text().splitlines()
    assert json.loads(log[-1])["thread"] == "existing"


def test_first_message_surface_says_none(site):
    cfg, port = site
    post(port, {"key": KEY, "item": "Brand new", "thread": "brand-new", "text": "hi"})
    (s,) = surfaces(cfg)
    assert "(none: this is the first message)" in s.read_text()


def test_page_name_sanitized(site):
    cfg, port = site
    post(
        port,
        {
            "key": KEY,
            "item": "x",
            "thread": "t",
            "text": "hi",
            "page": "../../evil\nfoo: bar",
        },
    )
    (s,) = surfaces(cfg)
    assert "page: evilfoobar\n" in s.read_text()


# ---------------------------------------------------------------- uploads


def test_upload_stored_privately_not_served(site, tmp_path):
    cfg, port = site
    data = base64.b64encode(b"PNGDATA").decode()
    status, _ = post(
        port,
        {
            "key": KEY,
            "item": "Pic",
            "thread": "pic",
            "text": "",
            "files": [{"name": "../../shot.png", "data": data}],
        },
    )
    assert status == 200
    stored = list((cfg.upload_dir / "pic").iterdir())
    assert len(stored) == 1 and stored[0].read_bytes() == b"PNGDATA"
    assert stored[0].name.endswith("shot.png") and ".." not in stored[0].name
    assert (cfg.upload_dir / "pic").stat().st_mode & 0o077 == 0
    doc = surfaces(cfg)[0].read_text()
    assert (
        f"/home/alice/alice-mind/inner/status-uploads/pic/{stored[0].name} (7 bytes)"
        in doc
    )
    assert "(no text: attachment only)" in doc
    msg = json.loads((cfg.threads_dir / "pic.jsonl").read_text().splitlines()[-1])
    assert msg["files"] == ["shot.png"]
    # Not reachable over HTTP by any route.
    for path in (
        f"/pic/{stored[0].name}",
        f"/uploads/pic/{stored[0].name}",
        f"/{stored[0].name}",
        "/../uploads/pic",
    ):
        assert req(port, "GET", path)[0] == 404


def test_upload_size_limits(site):
    cfg, port = site
    big = base64.b64encode(b"x" * (cfg.max_upload + 1)).decode()
    assert (
        post(
            port,
            {
                "key": KEY,
                "item": "x",
                "thread": "big",
                "files": [{"name": "a", "data": big}],
            },
        )[0]
        == 413
    )
    too_many = [{"name": f"f{i}", "data": "eA=="} for i in range(6)]
    assert (
        post(port, {"key": KEY, "item": "x", "thread": "many", "files": too_many})[0]
        == 413
    )
    bad = [{"name": "a", "data": "!!notbase64"}]
    assert (
        post(port, {"key": KEY, "item": "x", "thread": "bad", "files": bad})[0] == 400
    )
    # Whole-body cap: refused before reading.
    status, _, _ = req(
        port,
        "POST",
        "/api/reply",
        b"x" * (cfg.max_body + 1),
        {"Content-Type": "application/json"},
    )
    assert status == 413
    assert not cfg.upload_dir.exists() or not any(cfg.upload_dir.rglob("*"))
    assert surfaces(cfg) == []


def test_text_truncated(site):
    cfg, port = site
    post(port, {"key": KEY, "item": "x", "thread": "long", "text": "a" * 20000})
    msg = json.loads((cfg.threads_dir / "long.jsonl").read_text())
    assert len(msg["text"]) == 8000


# ---------------------------------------------------------------- static / traversal


def test_serves_packaged_frontend_and_content(site):
    cfg, port = site
    status, body, h = req(port, "GET", "/")
    assert (
        status == 200
        and b"marked.min.js" in body
        and h["Content-Type"].startswith("text/html")
    )
    assert "Last-Modified" in h
    assert not (
        cfg.status_dir / "index.html"
    ).exists()  # packaged, not from content dir
    assert req(port, "HEAD", "/index.html")[0] == 200
    assert req(port, "GET", "/marked.min.js")[0] == 200
    status, body, h = req(port, "GET", "/index.md?12345")
    assert status == 200 and body.startswith(b"# Overview") and "Last-Modified" in h
    assert req(port, "GET", "/pages.json")[0] == 200


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/..%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/threads/existing.jsonl",
        "/.replies.jsonl",
        "/threads%2fexisting.jsonl",
        "/index.md/../../secret.txt",
        "/etc/passwd",
        "/missing.md",
        "/server.py",
    ],
)
def test_traversal_and_private_files_404(site, path):
    _, port = site
    assert req(port, "GET", path)[0] == 404


def test_thread_traversal_neutralized(site, tmp_path):
    cfg, port = site
    status, j = post(
        port, {"key": KEY, "item": "x", "thread": "../../../evil", "text": "hi"}
    )
    assert status == 200 and j["thread"] == "evil"
    assert (cfg.threads_dir / "evil.jsonl").exists()
    assert not (tmp_path / "evil.jsonl").exists()


# ---------------------------------------------------------------- /api/threads


def test_api_threads_shape(site):
    cfg, port = site
    (cfg.threads_dir / "broken.jsonl").write_text(
        '{"from": "alice", "text": "ok"}\nnot json\n'
    )
    status, body, h = req(port, "GET", "/api/threads?123")
    assert status == 200 and h["Content-Type"] == "application/json"
    d = json.loads(body)
    assert set(d) == {"existing", "broken"}
    assert [m["from"] for m in d["existing"]] == ["jason", "alice"]
    assert all({"from", "text"} <= set(m) for m in d["existing"])
    assert d["broken"] == [{"from": "alice", "text": "ok"}]


# ---------------------------------------------------------------- CLI


def test_status_reply_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    assert cli.main(["Aimax-BIOS/../x", "  flashed  "]) == 0
    msg = json.loads((tmp_path / "threads" / "aimax-biosx.jsonl").read_text())
    assert msg["from"] == "alice" and msg["text"] == "flashed" and msg["at"]
    assert cli.main(["t", "   "]) == 2
    assert cli.main(["only-one"]) == 2


def test_config_from_env(tmp_path):
    key = tmp_path / "key"
    key.write_text("abc\n")
    cfg = Config.from_env(
        {
            "KEY_FILE": str(key),
            "STATUS_DIR": str(tmp_path / "s"),
            "UPLOAD_DIR": "/u",
            "PORT": "9000",
        }
    )
    assert cfg.key == "abc" and cfg.port == 9000 and cfg.upload_dir == Path("/u")
    assert cfg.upload_dir_alias == "/u"
