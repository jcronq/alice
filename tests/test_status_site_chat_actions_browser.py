"""Browser tests for chat permalinks / hover actions / replies (issue #668).

Needs ``playwright`` + a Chromium build and network access (the shell
imports React from esm.sh); skipped cleanly otherwise.
"""

from __future__ import annotations

import json
import threading

import pytest

from status_site._sse import SectionBroadcaster
from status_site.server import Config, make_server, section_renderers

sync_api = pytest.importorskip("playwright.sync_api")

KEY = "browser-key"
OLD = "2026-11-06T09:00:00-0500"
MID = "2026-11-06T09:01:00-0500"
NEW = "2026-11-06T09:02:00-0500"


@pytest.fixture
def base(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text("# Overview\n")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    lines = [{"from": "jason", "at": OLD, "text": "first message"}]
    lines += [
        {"from": "alice", "at": f"2026-11-06T09:00:{i:02d}-0500", "text": f"filler {i}\n" * 3}
        for i in range(1, 40)
    ]
    lines += [
        {"from": "jason", "at": MID, "text": "middle message to quote"},
        {"from": "alice", "at": NEW, "text": "latest message", "reply_to": MID},
    ]
    (status / ".chat.jsonl").write_text("".join(json.dumps(m) + "\n" for m in lines))
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
        sse_tick_seconds=0.2,
    )
    srv = make_server(cfg)
    cfg.broadcaster = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.2)
    cfg.broadcaster.start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, f"http://127.0.0.1:{srv.server_address[1]}"
    cfg.broadcaster.stop.set()
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def page(base):
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser installed
            pytest.skip(f"chromium unavailable: {e}")
        ctx = browser.new_context(
            viewport={"width": 1000, "height": 700},
            permissions=["clipboard-read", "clipboard-write"],
        )
        pg = ctx.new_page()
        try:
            pg.goto(base[1] + f"/?k={KEY}#chat", wait_until="domcontentloaded")
            pg.wait_for_selector(".msg", timeout=15000)
        except Exception as e:  # noqa: BLE001 - esm.sh unreachable
            pytest.skip(f"shell did not load (offline?): {e}")
        yield pg, base[0], base[1]
        browser.close()


def in_view(pg, at):
    return pg.evaluate(
        """at => {
          const el = document.getElementById('msg-' + at);
          const t = document.querySelector('.a2ui-chat-thread').getBoundingClientRect();
          const r = el.getBoundingClientRect();
          return r.top >= t.top - 1 && r.bottom <= t.bottom + 1;
        }""",
        at,
    )


def test_permalink_scrolls_into_view_and_highlights(page):
    pg, _, base = page
    pg.goto(base + f"/?p=chat#msg-{MID}")
    pg.wait_for_selector(f'[id="msg-{MID}"]')
    pg.wait_for_function(f"document.getElementById('msg-{MID}').classList.contains('highlight')")
    assert in_view(pg, MID)
    assert pg.url.endswith(f"#msg-{MID}")
    pg.wait_for_function(
        f"!document.getElementById('msg-{MID}').classList.contains('highlight')", timeout=4000
    )


def test_react_shows_pill_and_updates_sidecar(page):
    pg, cfg, _ = page
    msg = pg.locator(f'[id="msg-{NEW}"]')
    msg.scroll_into_view_if_needed()
    msg.hover()
    msg.locator('[data-testid="react-btn"][data-emoji="👍"]').click()
    pill = msg.locator('[data-testid="react-pill"]')
    pill.wait_for()
    assert pill.inner_text().strip() == "👍 1"
    events = [json.loads(ln) for ln in cfg.chat_reactions_log.read_text().splitlines()]
    assert [(e["at"], e["emoji"], e["action"]) for e in events] == [(NEW, "👍", "add")]
    pill.click()  # clicking your own pill removes it
    pg.wait_for_function(f"!document.getElementById('msg-{NEW}').querySelector('[data-testid=react-pill]')")
    assert json.loads(cfg.chat_reactions_log.read_text().splitlines()[-1])["action"] == "remove"


def test_copy_link_puts_permalink_on_clipboard(page):
    pg, _, base = page
    msg = pg.locator(f'[id="msg-{NEW}"]')
    msg.hover()
    msg.locator('[data-testid="copy-link-btn"]').click()
    pg.wait_for_function("navigator.clipboard.readText().then(t => t.includes('#msg-'))")
    assert pg.evaluate("navigator.clipboard.readText()") == f"{base}/?p=chat#msg-{NEW}"


def test_reply_prefills_composer_and_sends_reply_to(page):
    pg, cfg, _ = page
    msg = pg.locator(f'[id="msg-{MID}"]')
    msg.scroll_into_view_if_needed()
    msg.hover()
    msg.locator('[data-testid="reply-btn"]').click()
    ta = pg.locator(".a2ui-composer textarea")
    assert ta.input_value() == "> middle message to quote\n\n"
    assert pg.locator('[data-testid="reply-chip"]').count() == 1
    ta.type("agreed")
    pg.locator(".a2ui-composer-send button").click()
    pg.wait_for_function("document.querySelectorAll('.msg').length > 41")
    last = [json.loads(ln) for ln in cfg.chat_log.read_text().splitlines()][-1]
    assert last["reply_to"] == MID and last["text"].endswith("agreed")


def test_reply_header_jumps_to_quoted_message(page):
    pg, _, _ = page
    ref = pg.locator(f'[id="msg-{NEW}"] [data-testid="reply-ref"]')
    assert ref.inner_text() == "In reply to: middle message to quote"
    pg.evaluate("document.querySelector('.a2ui-chat-thread').scrollTop = 0")
    ref.scroll_into_view_if_needed()
    ref.click()
    pg.wait_for_function(f"document.getElementById('msg-{MID}').classList.contains('highlight')")
    assert in_view(pg, MID)


# ---- regression: long unbroken tokens must wrap on mobile (#805) ----

LONG_TOKEN = "https://example.com/" + ("a" * 180)  # 200-char unbroken token


@pytest.fixture
def base_long_token(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text("# Overview\n")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    lines = [{"from": "jason", "at": OLD, "text": LONG_TOKEN}]
    (status / ".chat.jsonl").write_text("".join(json.dumps(m) + "\n" for m in lines))
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
        sse_tick_seconds=0.2,
    )
    srv = make_server(cfg)
    cfg.broadcaster = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.2)
    cfg.broadcaster.start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, f"http://127.0.0.1:{srv.server_address[1]}"
    cfg.broadcaster.stop.set()
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def page_mobile(base_long_token):
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser installed
            pytest.skip(f"chromium unavailable: {e}")
        ctx = browser.new_context(
            viewport={"width": 375, "height": 812},
        )
        pg = ctx.new_page()
        try:
            pg.goto(base_long_token[1] + f"/?k={KEY}#chat", wait_until="domcontentloaded")
            pg.wait_for_selector(".msg", timeout=15000)
        except Exception as e:  # noqa: BLE001 - esm.sh unreachable
            pytest.skip(f"shell did not load (offline?): {e}")
        yield pg, base_long_token[0], base_long_token[1]
        browser.close()


def test_long_unbroken_token_wraps_on_mobile(page_mobile):
    pg, _, _ = page_mobile
    no_overflow = pg.evaluate(
        "() => { const el = document.querySelector('.a2ui-chat-thread');"
        "  return el.scrollWidth <= el.clientWidth + 1; }"
    )
    assert no_overflow, "chat thread overflows horizontally on 375px viewport"
