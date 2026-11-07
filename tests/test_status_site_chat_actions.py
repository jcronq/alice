"""Chat message permalinks, reactions, replies and queue deep-links (issue #668)."""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import threading

import pytest

from status_site import chat_reactions
from status_site._sse import SectionBroadcaster
from status_site.server import (
    STATIC_DIR,
    Config,
    make_server,
    render_alice_queue,
    scan_surfaces,
    section_renderers,
)
from status_site.sources.chat import chat_preview

KEY = "react-key-xyz"
T1 = "2026-11-06T11:46:00-0500"
T2 = "2026-11-06T11:47:00-0500"


@pytest.fixture
def site(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text("# Overview\n")
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
        upload_dir_alias="/home/alice/alice-mind/inner/status-uploads",
    )
    srv = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    c.request(method, path, body=data, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    c.close()
    return out


def seed_chat(cfg):
    cfg.chat_log.write_text(
        json.dumps({"from": "jason", "at": T1, "text": "hello"})
        + "\n"
        + json.dumps({"from": "alice", "at": T2, "text": "hi back"})
        + "\n"
    )


def react(port, **over):
    body = {"key": KEY, "at": T1, "emoji": "👍", "action": "add", **over}
    return req(port, "POST", "/api/chat/react", body)


def get_msgs(port):
    return req(port, "GET", "/api/chat")[1]["messages"]


# ------------------------------------------------------------ preview


def body_for(text, attachments="  none"):
    return (
        "# Jason sent a message on the Chat tab\n\nHis new message:\n"
        f"{text}\n\nAttachments (Read them; stored privately, not served):\n"
        f"{attachments}\n\nRecent chat (for context):\n  (none)\n"
    )


def test_preview_plain_text():
    assert chat_preview(body_for("can you look at the build?")) == "can you look at the build?"


def test_preview_truncates_with_ellipsis():
    out = chat_preview(body_for("word " * 60))
    assert len(out) == 80 and out.endswith("…")
    assert chat_preview(body_for("x" * 80)) == "x" * 80


def test_preview_collapses_whitespace_and_strips_html():
    out = chat_preview(body_for("line one\n\nline <b>two</b>\n<script>alert(1)</script>"))
    assert out == "line one line two alert(1)"
    assert "<" not in out
    assert chat_preview(body_for("a < b and c > d")) == "a < b and c > d"


def test_preview_with_attachment_keeps_text():
    assert chat_preview(body_for("see this", "  /x/shot.png (3 bytes)")) == "see this"


def test_preview_attachment_only():
    assert chat_preview(body_for("(no text: attachment only)", "  /x/a.png (3 bytes)")) == "📎 attachment"


def test_preview_unrecognised_body_is_empty():
    assert chat_preview("just some other surface") == ""
    assert chat_preview("") == ""


# ------------------------------------------------------ merge_reactions


def test_merge_tolerates_missing_sidecar(tmp_path):
    msgs = [{"at": T1, "text": "x"}]
    assert chat_reactions.merge_reactions(msgs, tmp_path / "nope.jsonl") == [{"at": T1, "text": "x"}]


def test_merge_tolerates_malformed_and_stale_lines(tmp_path):
    p = tmp_path / ".chat-reactions.jsonl"
    p.write_text(
        "not json\n"
        "[1, 2]\n"
        + json.dumps({"at": T1, "emoji": "👍"}) + "\n"  # no "from"
        + json.dumps({"at": T1, "emoji": "", "from": "jason"}) + "\n"
        + json.dumps({"at": T1, "emoji": "a b", "from": "jason"}) + "\n"
        + json.dumps({"at": T1, "emoji": "🎉", "from": "jason", "action": "nuke"}) + "\n"
        + json.dumps({"at": "1999-01-01T00:00:00+0000", "emoji": "👍", "from": "jason"}) + "\n"
        + json.dumps({"at": T1, "emoji": "👍", "from": "jason", "at_reacted": "r1"}) + "\n"
    )
    msgs = chat_reactions.merge_reactions([{"at": T1}, {"at": T2}, {"text": "no at"}], p)
    assert msgs[0]["reactions"] == {"👍": [{"from": "jason", "at": "r1"}]}
    assert "reactions" not in msgs[1] and "reactions" not in msgs[2]


def test_merge_replays_add_then_remove(tmp_path):
    p = tmp_path / "r.jsonl"
    ev = lambda action, who="jason": json.dumps(  # noqa: E731
        {"at": T1, "emoji": "👍", "from": who, "at_reacted": "t", "action": action}
    )
    p.write_text("\n".join([ev("add"), ev("add", "alice"), ev("remove")]) + "\n")
    [m] = chat_reactions.merge_reactions([{"at": T1}], p)
    assert m["reactions"] == {"👍": [{"from": "alice", "at": "t"}]}
    p.write_text("\n".join([ev("add"), ev("remove")]) + "\n")
    [m] = chat_reactions.merge_reactions([{"at": T1}], p)
    assert "reactions" not in m


# ---------------------------------------------------- POST /api/chat/react


def test_react_requires_key(site):
    cfg, port = site
    seed_chat(cfg)
    assert react(port, key="wrong")[0] == 403
    assert not cfg.chat_reactions_log.exists()


@pytest.mark.parametrize(
    "over, code",
    [
        ({"emoji": ""}, 400),
        ({"emoji": "two words"}, 400),
        ({"emoji": "x" * 40}, 400),
        ({"emoji": 7}, 400),
        ({"action": "toggle"}, 400),
        ({"at": ""}, 400),
        ({"at": "2020-01-01T00:00:00+0000"}, 404),
    ],
)
def test_react_validates_payload(site, over, code):
    cfg, port = site
    seed_chat(cfg)
    assert react(port, **over)[0] == code
    assert not cfg.chat_reactions_log.exists()


def test_react_add_writes_sidecar_and_get_chat_echoes_it(site):
    cfg, port = site
    seed_chat(cfg)
    before = cfg.chat_log.read_text()
    status, body = react(port)
    assert status == 200 and body["ok"] is True
    assert list(body["reactions"]) == ["👍"]
    [line] = cfg.chat_reactions_log.read_text().splitlines()
    ev = json.loads(line)
    assert (ev["at"], ev["emoji"], ev["from"]) == (T1, "👍", "jason")
    assert ev["at_reacted"]
    assert cfg.chat_log.read_text() == before  # never rewritten in place
    m1, m2 = get_msgs(port)
    assert m1["reactions"]["👍"][0]["from"] == "jason"
    assert "reactions" not in m2


def test_react_add_is_idempotent_and_remove_clears_it(site):
    cfg, port = site
    seed_chat(cfg)
    react(port)
    react(port)
    assert len(cfg.chat_reactions_log.read_text().splitlines()) == 1
    react(port, emoji="❤️")
    status, body = react(port, action="remove")
    assert status == 200 and list(body["reactions"]) == ["❤️"]
    assert list(get_msgs(port)[0]["reactions"]) == ["❤️"]
    react(port, action="remove")  # already gone: no-op
    react(port, emoji="❤️", action="remove")
    assert "reactions" not in get_msgs(port)[0]


def test_reactions_sidecar_is_not_served(site):
    cfg, port = site
    seed_chat(cfg)
    react(port)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/.chat-reactions.jsonl")
    assert c.getresponse().status == 404


def _find_messages(env):
    def walk(o):
        if isinstance(o, list) and o and isinstance(o[0], dict) and "at" in o[0]:
            yield from o
        elif isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
    return walk(env)


def test_reactions_flow_into_the_chat_a2ui_payload(site):
    cfg, port = site
    seed_chat(cfg)
    react(port)
    b = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.05)
    try:
        envs = b.snapshot()["sections"]["page:chat"]["a2ui"]
    finally:
        b.stop.set()
    msgs = json.dumps(envs[-1])
    assert "reactions" in msgs
    msgs = [m for m in _find_messages(envs[-1])]
    assert msgs[0]["reactions"]["👍"][0]["from"] == "jason"


# ------------------------------------------------------------ reply_to


def test_chat_post_stores_reply_to(site):
    cfg, port = site
    seed_chat(cfg)
    assert req(port, "POST", "/api/chat", {"key": KEY, "text": "agreed", "reply_to": T2})[0] == 200
    assert req(port, "POST", "/api/chat", {"key": KEY, "text": "plain"})[0] == 200
    *_, replied, plain = get_msgs(port)
    assert replied["reply_to"] == T2
    assert "reply_to" not in plain


def test_chat_post_stores_client_message_id(site):
    cfg, port = site
    seed_chat(cfg)
    assert req(
        port, "POST", "/api/chat",
        {"key": KEY, "text": "with id", "client_message_id": "abc123"},
    )[0] == 200
    assert req(port, "POST", "/api/chat", {"key": KEY, "text": "no id"})[0] == 200
    *_, tagged, plain = get_msgs(port)
    assert tagged["client_message_id"] == "abc123"
    assert "client_message_id" not in plain


def test_chat_post_truncates_long_client_message_id(site):
    from status_site.server import MAX_CLIENT_MSG_ID

    cfg, port = site
    seed_chat(cfg)
    long_id = "z" * 250
    assert req(
        port, "POST", "/api/chat",
        {"key": KEY, "text": "long id", "client_message_id": long_id},
    )[0] == 200
    *_, tagged = get_msgs(port)
    assert tagged["client_message_id"] == "z" * MAX_CLIENT_MSG_ID
    assert len(tagged["client_message_id"]) == MAX_CLIENT_MSG_ID


# ------------------------------------------------- queue preview + link


def test_chat_post_surface_carries_message_at_and_queue_deep_links(site):
    cfg, port = site
    assert req(port, "POST", "/api/chat", {"key": KEY, "text": "ship it please"})[0] == 200
    [msg] = get_msgs(port)
    [item] = scan_surfaces(cfg.surface_dir)
    assert item["message_at"] == msg["at"]
    assert item["preview"] == "ship it please"

    b = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.05)
    try:
        [row] = b.snapshot()["sections"]["alice_queue"]["rows"]
    finally:
        b.stop.set()
    assert row["link"] == f"?p=chat#msg-{msg['at']}"
    assert row["preview"] == "ship it please"

    md = render_alice_queue(scan_surfaces(cfg.surface_dir))
    assert f"[open chat](?p=chat#msg-{msg['at']})" in md
    assert "ship it please" in md


def test_prior_schema_chat_surface_falls_back_to_plain_link(site):
    cfg, port = site
    cfg.surface_dir.mkdir(parents=True)
    (cfg.surface_dir / "2026-11-06T10-00-00-status-chat.md").write_text(
        "---\ntype: status-chat\nsource: status-site\npage: chat\n---\n\n"
        "# Jason sent a message on the Chat tab\n\nHis new message:\nold one\n\n"
        "Attachments (Read them):\n  none\n"
    )
    [item] = scan_surfaces(cfg.surface_dir)
    assert item["message_at"] == ""
    assert item["preview"] == "old one"
    b = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.05)
    try:
        [row] = b.snapshot()["sections"]["alice_queue"]["rows"]
    finally:
        b.stop.set()
    assert row["link"] == "?p=chat"


def test_api_surfaces_never_exposes_previews(site):
    cfg, port = site
    req(port, "POST", "/api/chat", {"key": KEY, "text": "private words"})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", f"/api/surfaces?k={KEY}")
    r = c.getresponse()
    raw = r.read().decode()
    assert r.status == 200 and "slug" in raw
    assert "private words" not in raw


# ------------------------------------------------------- client helpers

NODE_SCRIPT = r"""
import * as ux from %(mod)s;
const out = {};
out.id = ux.messageAnchorId("2026-11-06T11:46:00-0500");
out.hash = ux.parseMessageHash("#msg-2026-11-06T11:46:00-0500");
out.hashPlus = ux.parseMessageHash("#msg-2026-11-06T11:46:00+0000");
out.hashEncoded = ux.parseMessageHash("#msg-2026-11-06T11%%3A46%%3A00-0500");
out.hashNone = [ux.parseMessageHash("#chat"), ux.parseMessageHash(""), ux.parseMessageHash("#msg-")];
out.link = ux.permalinkFor("https://10.20.30.177:8095", "2026-11-06T11:46:00-0500");
out.previewText = ux.replyPreview({text: "  hello\n  world  "});
out.previewLong = ux.replyPreview({text: "y".repeat(200)}).length;
out.previewAtt = ux.replyPreview({text: "", attachments: [{name: "a.png"}]});
out.previewNone = ux.replyPreview({text: ""});
out.quote = ux.quoteForReply({text: "first\n\nthird"});
out.quoteAtt = ux.quoteForReply({text: "", files: ["a.png"]});
out.quoteLong = ux.quoteForReply({text: "z".repeat(1000)}).length;
out.summary = ux.summarizeReactions(
  {"👍": [{from: "jason"}, {from: "alice"}], "🎉": [{from: "alice"}], "👀": []});
out.sigSame = ux.reactionsSignature({"👍": [{from: "b"}, {from: "a"}]}) ===
  ux.reactionsSignature({"👍": [{from: "a"}, {from: "b"}], "👀": []});
out.quick = ux.QUICK_REACTS;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    mod = json.dumps((STATIC_DIR / "chat-ux.js").as_uri())
    script = tmp_path_factory.mktemp("node") / "t.mjs"
    script.write_text(NODE_SCRIPT % {"mod": mod})
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_js_anchor_and_hash_helpers(js):
    assert js["id"] == "msg-2026-11-06T11:46:00-0500"
    assert js["hash"] == "2026-11-06T11:46:00-0500"
    assert js["hashPlus"] == "2026-11-06T11:46:00+0000"
    assert js["hashEncoded"] == "2026-11-06T11:46:00-0500"
    assert js["hashNone"] == ["", "", ""]
    assert js["link"] == "https://10.20.30.177:8095/?p=chat#msg-2026-11-06T11:46:00-0500"


def test_js_reply_helpers(js):
    assert js["previewText"] == "hello world"
    assert js["previewLong"] == 80
    assert js["previewAtt"] == "📎 attachment"
    assert js["previewNone"] == ""
    assert js["quote"] == "> first\n>\n> third\n\n"
    assert js["quoteAtt"] == "> 📎 attachment\n\n"
    assert js["quoteLong"] <= 300 + 4


def test_js_reaction_helpers(js):
    assert js["summary"] == [
        {"emoji": "👍", "count": 2, "mine": True},
        {"emoji": "🎉", "count": 1, "mine": False},
    ]
    assert js["sigSame"] is True
    assert js["quick"] == ["👍", "❤️", "😂", "🎉", "👀"]


def test_shell_wires_permalinks_reactions_and_replies(site):
    _, port = site
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/index.html")
    html = c.getresponse().read().decode()
    for needle in (
        "messageAnchorId(m.at)",
        "/api/chat/react",
        "msg-actions",
        "scrollIntoView({block: \"center\"})",
        "parseMessageHash(location.hash)",
        "copy-link-btn",
        "reply-ref",
        "More actions — coming soon",
        "reply_to",
    ):
        assert needle in html, needle
    css = (STATIC_DIR / "design-tokens.css").read_text()
    assert ".msg.highlight" in css and ".msg-actions" in css


# ------------------------------------------- attention-react surface (#683)


def _react_surfaces(cfg):
    return sorted(cfg.surface_dir.glob("*-chat-reaction*.md")) if cfg.surface_dir.is_dir() else []


def test_attention_react_on_alice_message_drops_one_surface(site):
    cfg, port = site
    seed_chat(cfg)
    assert react(port, at=T2, emoji="👀")[0] == 200
    [p] = _react_surfaces(cfg)
    doc = p.read_text()
    assert "type: chat-reaction" in doc and "priority: wander" in doc
    assert "reply_expected: false" in doc
    assert "hi back" in doc and "👀" in doc and f"?p=chat#msg-{T2}" in doc
    # duplicate add is a no-op, and cooldown blocks add/remove/add spam
    react(port, at=T2, emoji="👀")
    react(port, at=T2, emoji="👀", action="remove")
    react(port, at=T2, emoji="👀")
    assert len(_react_surfaces(cfg)) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"at": T2, "emoji": "👍"},  # ack-only
        {"at": T1, "emoji": "👀"},  # jason's own message
    ],
)
def test_non_attention_react_drops_nothing(site, over):
    cfg, port = site
    seed_chat(cfg)
    assert react(port, **over)[0] == 200
    assert _react_surfaces(cfg) == []


def test_remove_react_drops_nothing(site):
    cfg, port = site
    seed_chat(cfg)
    react(port, at=T2, emoji="❓")
    n = len(_react_surfaces(cfg))
    react(port, at=T2, emoji="❓", action="remove")
    assert len(_react_surfaces(cfg)) == n
