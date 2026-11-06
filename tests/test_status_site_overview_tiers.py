"""Overview split by who owns the next action (issue #691).

Unit: ``split_by_owner`` and friends. Integration: the four new
``section.update`` payloads carry typed rows. Static: the shell's tier
order. Browser (skipped without playwright + chromium + network):
TOP is first, MIDDLE is info-only, BOTTOM renders, at light/dark and
1280/480.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from alice_forge.review_queue_renderer import DesignItem, PRItem
from status_site._sse import SectionBroadcaster
from status_site.server import (
    Config,
    OverviewPollers,
    make_server,
    section_renderers,
    thinking_queue_rows,
)
from status_site.sources import pipeline as P

STATIC = Path(__file__).resolve().parent.parent / "src" / "status_site" / "static"
NOW = 1_800_000_000.0


def _row(n, state, age=None, repo="jcronq/alice"):
    return {"repo": repo, "number": n, "title": f"t{n}", "state": state, "age_seconds": age}


# ─── split_by_owner ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,owner",
    [
        ("design_review", "jason"),
        ("selected", "alice"),
        ("designing", "alice"),
        ("designed", "alice"),
        ("building", "alice"),
        ("reviewing", "alice"),
        ("blocked", "alice"),
        ("something-new", "alice"),
    ],
)
def test_split_by_owner_routes_each_state(state, owner):
    out = P.split_by_owner([_row(1, state)])
    other = "alice" if owner == "jason" else "jason"
    assert [r["number"] for r in out[owner]] == [1]
    assert out[other] == []


def test_split_by_owner_mixed_and_empty():
    assert P.split_by_owner([]) == {"jason": [], "alice": []}
    rows = [_row(i, s) for i, s in enumerate(P.PIPELINE_STATES, 1)]
    out = P.split_by_owner(rows)
    assert [r["state"] for r in out["jason"]] == ["design_review"]
    assert len(out["alice"]) == len(P.PIPELINE_STATES) - 1


def test_blocks_sort_oldest_first_unknown_age_last():
    out = P.split_by_owner(
        [
            _row(1, "building", age=10),
            _row(2, "building", age=None),
            _row(3, "building", age=500),
            _row(4, "reviewing", age=500),
        ]
    )
    # tie on age → pipeline state order (building before reviewing)
    assert [r["number"] for r in out["alice"]] == [3, 4, 1, 2]


def test_age_label_seconds():
    assert P.age_label_seconds("<1h") == 1800.0
    assert P.age_label_seconds("5h") == 5 * 3600
    assert P.age_label_seconds("3d") == 3 * 86400
    assert P.age_label_seconds("") is None
    assert P.age_label_seconds("soon") is None


def test_merge_design_review_dedupes_preferring_pipeline_row():
    pipeline_rows = [_row(7, "design_review", age=100)]
    review = [
        DesignItem("jcronq/alice", 7, "dupe", "https://x/7", "1d"),
        DesignItem("jcronq/alice", 9, "extra", "https://x/9", "2d"),
    ]
    merged = P.merge_design_review(pipeline_rows, review)
    assert [(r["number"], r["age_seconds"]) for r in merged] == [(9, 2 * 86400), (7, 100)]
    assert merged[1]["title"] == "t7"
    assert merged[0]["link"] == "https://x/9"


# ─── section payloads ──────────────────────────────────────────────


def _cfg(tmp_path, **kw):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True, exist_ok=True)
    (status / "index.md").write_text("# Overview\n")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    return Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
        **kw,
    )


def _seed(cfg):
    cfg.pollers = OverviewPollers(cfg)
    cfg.pollers._set(
        "review_queue",
        {
            "design": [DesignItem("jcronq/alice", 50, "needs design look", "https://x/50", "2d")],
            "pr": [
                PRItem("jcronq/alice", 70, "new PR", "https://x/70", "<1h"),
                PRItem("jcronq/alice", 71, "old PR", "https://x/71", "3d"),
            ],
            "design_failed": [],
            "pr_failed": ["other/repo"],
        },
    )
    issues = [
        {"repo": "jcronq/alice", "number": 50, "title": "needs design look",
         "url": "https://x/50", "state": "design_review", "art": "", "updated_epoch": time.time() - 3600},
        {"repo": "jcronq/alice", "number": 60, "title": "building it",
         "url": "https://x/60", "state": "building", "art": "code", "updated_epoch": time.time() - 60},
    ]
    cfg.pollers._set("pipeline", {"issues": issues, "failed": []})
    (cfg.status_dir / "waiting-on-you.md").write_text("Call Randy about the thing")
    (cfg.status_dir / "external-todos.md").write_text("- Bambu LAN code")
    (cfg.status_dir / "known-issues.md").write_text("flaky thing")
    (cfg.status_dir / "asks.jsonl").write_text(
        json.dumps({"id": "a1", "text": "pick a color", "created": "2026-11-06T09:00:00-0500"}) + "\n"
    )


def test_your_todo_payload_collects_everything_blocking_jason(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    cfg = _cfg(tmp_path)
    _seed(cfg)
    data = section_renderers(cfg)["your_todo"]()

    assert [r["number"] for r in data["prs"]] == [71, 70]  # oldest first
    # sm:design_review issue appears once even though both sources list it.
    assert [r["number"] for r in data["design_review"]] == [50]
    assert data["your_todo_md"] == "Call Randy about the thing"
    assert data["external_todos_md"] == "- Bambu LAN code"
    assert data["attention_reacts"] == []
    assert data["failed"] == {"prs": ["other/repo"], "design_review": []}
    assert data["counts"]["prs"] == 2 and data["counts"]["design_review"] == 1
    assert "markdown" in data
    # Building work is Alice's, so it must not leak into Jason's block.
    assert all(r["state"] != "building" for r in data["design_review"])


def test_in_flight_payload_is_alice_owned_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cfg.surface_dir.mkdir()
    (cfg.surface_dir / "2026-11-06T10-00-00-one.md").write_text(
        "---\ntype: status-chat\nsource: status-site\n---\nbody\n"
    )
    data = section_renderers(cfg)["in_flight"]()
    assert [r["number"] for r in data["pipeline"]] == [60]
    assert [s["slug"] for s in data["surfaces"]] == ["one"]
    assert data["running"] == [] and data["tasks"] == []


def test_system_health_and_thinking_queue_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    cfg = _cfg(tmp_path)
    _seed(cfg)
    (cfg.status_dir / ".thinking-queue.json").write_text(
        json.dumps(
            {
                "synced_at": time.time(),
                "items": [
                    {"number": 3, "title": "idea", "labels": [{"name": "sm:designing"}],
                     "updatedAt": "2026-11-06T10:00:00Z"},
                ],
            }
        )
    )
    r = section_renderers(cfg)
    health = r["system_health"]()
    assert health["known_issues_md"] == "flaky thing"
    assert len(health["deploy"]) == 1 and health["deploy"][0]["host"]
    assert {p["name"] for p in health["pollers"]} == {"review_queue", "pipeline"}
    tq = r["thinking_queue"]()
    assert tq["available"] and [x["number"] for x in tq["rows"]] == [3]
    assert tq["rows"][0]["phase"] == "Designing"


def test_thinking_queue_rows_unavailable_when_stale_or_missing():
    assert thinking_queue_rows(None)["available"] is False
    stale = {"synced_at": 0.0, "items": []}
    assert thinking_queue_rows(stale, now=10_000.0)["available"] is False


def test_new_sections_arrive_in_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    cfg = _cfg(tmp_path)
    _seed(cfg)
    b = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.05)
    try:
        sections = b.snapshot()["sections"]
    finally:
        b.stop.set()
    for name in ("your_todo", "in_flight", "done_today", "thinking_queue", "system_health"):
        assert "markdown" in sections[name]
    # Legacy per-source sections keep serving /index.md splices.
    assert {"running_now", "waiting_on_you", "alice_queue"} <= set(sections)


# ─── static shell ──────────────────────────────────────────────────


def test_shell_tiers_are_ordered_and_widgets_renamed():
    html = (STATIC / "index.html").read_text()
    assert "AliceQueueList" not in html
    for widget in ("YourTodoBoard", "InFlightBoard", "PipelineBoard", "FxTable", "FreshnessTile"):
        assert f"function {widget}(" in html
    order = [html.index(f'id: "{t}"') for t in ("top", "middle", "bottom", "thinking", "health")]
    assert order == sorted(order)
    assert 'sections: ["your_todo"]' in html and 'sections: ["in_flight"]' in html


# ─── browser ───────────────────────────────────────────────────────

KEY = "tier-key"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    cfg = _cfg(tmp_path, sse_tick_seconds=0.2)
    cfg.key = KEY
    _seed(cfg)
    srv = make_server(cfg)
    cfg.broadcaster = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.2)
    cfg.broadcaster.start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    cfg.broadcaster.stop.set()
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 480])
def test_overview_tiers_render_in_browser(served, scheme, width):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no browser installed
            pytest.skip(f"chromium unavailable: {e}")
        ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
        pg = ctx.new_page()
        try:
            pg.goto(f"{served}/?k={KEY}&theme={scheme}", wait_until="domcontentloaded")
            pg.wait_for_selector('[data-tier="top"] [data-board="your-todo"]', timeout=15000)
        except Exception as e:  # noqa: BLE001 - esm.sh unreachable
            browser.close()
            pytest.skip(f"shell did not load (offline?): {e}")
        pg.wait_for_selector('[data-tier="middle"] [data-board="in-flight"]')
        pg.wait_for_selector('[data-tier="bottom"] [data-section="done_today"]')

        tops = pg.evaluate(
            """() => ['top','middle','bottom','thinking','health'].map(id =>
                 document.querySelector(`[data-tier="${id}"]`).getBoundingClientRect().top)"""
        )
        assert tops == sorted(tops) and len(set(tops)) == 5

        # TOP: PRs, design review, asks and external TODOs all present and open.
        top_text = pg.inner_text('[data-tier="top"]')
        for needle in ("new PR", "old PR", "needs design look", "pick a color",
                       "Call Randy", "Bambu LAN code"):
            assert needle in top_text
        assert pg.locator('[data-tier="top"] details:not([open])').count() == 0

        # MIDDLE is info-only: nothing Jason can press.
        assert pg.locator('[data-tier="middle"] button, [data-tier="middle"] input').count() == 0
        assert "building it" in pg.inner_text('[data-tier="middle"]')

        # Asides are collapsed disclosures.
        for tid in ("thinking", "health"):
            assert pg.get_attribute(f'[data-tier="{tid}"]', "open") is None
        browser.close()
