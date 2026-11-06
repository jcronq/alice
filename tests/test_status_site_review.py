"""Review page: ``/api/reviews``, ``/api/review``, ``/api/review-action`` (issue #616)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from status_site.server import Config, make_server

KEY = "review-key-xyz"

PLAN_NOTE = """---
kind: decide
needs: review
repo: acme/widgets
issue: 616
title: "Review page for plans"
url: https://github.com/acme/widgets/issues/616
---

# Design review — acme/widgets#616

## Shape
Dedicated **Review page**.
"""


@pytest.fixture
def site(tmp_path):
    status = tmp_path / "status"
    notes = status / "notes"
    notes.mkdir(parents=True)
    (status / "threads").mkdir()
    (status / "index.md").write_text("# Overview\n")
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    (notes / "design-review-acme-widgets-616.md").write_text(PLAN_NOTE)
    (notes / "needs-only.md").write_text("---\nneeds: review\n---\nbody\n")
    (notes / "plain-note.md").write_text("---\nkind: summary\n---\nnot a review\n")
    (notes / "no-frontmatter.md").write_text("just text\n")
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
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


def act(port, **kw):
    return req(port, "POST", "/api/review-action", kw)


def surfaces(cfg):
    return sorted(cfg.surface_dir.glob("*.md")) if cfg.surface_dir.exists() else []


SLUG = "design-review-acme-widgets-616"


def test_api_reviews_filters_on_frontmatter(site):
    _, port = site
    status, body = req(port, "GET", "/api/reviews")
    assert status == 200
    assert body == [SLUG, "needs-only"]


def test_api_reviews_empty_without_notes_dir(site):
    cfg, port = site
    for p in (cfg.status_dir / "notes").glob("*.md"):
        p.unlink()
    assert req(port, "GET", "/api/reviews") == (200, [])


def test_api_review_renders_plan_body(site):
    _, port = site
    status, body = req(port, "GET", f"/api/review?slug={SLUG}")
    assert status == 200
    assert body["title"] == "Review page for plans"
    assert body["repo"] == "acme/widgets" and body["issue"] == "616"
    assert "## Shape" in body["markdown"]
    assert "kind: decide" not in body["markdown"]


def test_api_review_404_for_non_review_note(site):
    _, port = site
    assert req(port, "GET", "/api/review?slug=plain-note")[0] == 404
    assert req(port, "GET", "/api/review?slug=missing")[0] == 404
    assert req(port, "GET", "/api/review?slug=")[0] == 404


@pytest.mark.parametrize("action", ["approve", "modify", "reject"])
def test_each_action_drops_typed_surface_with_notes(site, action):
    cfg, port = site
    status, body = act(port, key=KEY, slug=SLUG, action=action, notes="please do X")
    assert status == 200 and body["ok"] and body["action"] == action
    (f,) = surfaces(cfg)
    assert f.name.endswith(f"status-review-{action}-{SLUG}.md")
    doc = f.read_text()
    assert f"type: status-review-{action}" in doc
    assert f"action: {action}" in doc
    assert "repo: acme/widgets" in doc and "issue: 616" in doc
    assert "please do X" in doc
    assert "acme/widgets#616" in doc


def test_bad_key_is_403_and_drops_nothing(site):
    cfg, port = site
    assert act(port, slug=SLUG, action="approve")[0] == 403
    assert act(port, key="wrong", slug=SLUG, action="approve")[0] == 403
    assert act(port, key=KEY[:-1], slug=SLUG, action="approve")[0] == 403
    assert surfaces(cfg) == []


def test_bad_action_unknown_slug_and_non_review_note(site):
    cfg, port = site
    assert act(port, key=KEY, slug=SLUG, action="ship")[0] == 400
    assert act(port, key=KEY, slug="missing", action="approve")[0] == 404
    assert act(port, key=KEY, slug="plain-note", action="approve")[0] == 404
    assert surfaces(cfg) == []


def test_modify_requires_notes_but_approve_and_reject_do_not(site):
    cfg, port = site
    assert act(port, key=KEY, slug=SLUG, action="modify", notes="  ")[0] == 400
    assert surfaces(cfg) == []
    assert act(port, key=KEY, slug=SLUG, action="approve")[0] == 200
    assert act(port, key=KEY, slug=SLUG, action="reject")[0] == 200
    assert "(no notes)" in surfaces(cfg)[0].read_text()


def test_item_stays_reviewable_after_action(site):
    _, port = site
    assert act(port, key=KEY, slug=SLUG, action="approve")[0] == 200
    assert SLUG in req(port, "GET", "/api/reviews")[1]
    assert req(port, "GET", f"/api/review?slug={SLUG}")[0] == 200


# ---------------------------------------------------------------- A2UI emitter (#651)


def test_review_a2ui_empty_before_slug_is_set(site):
    """With no active slug the renderer emits an empty envelope list so
    the SSE tick's dict-equality compare suppresses noise.
    """
    from status_site.sources import review as review_page

    cfg, _ = site
    review_page.set_active_slug(None)
    assert review_page.render(cfg) == {"a2ui": []}


def test_review_a2ui_emits_surface_after_api_review_sets_slug(site):
    """``GET /api/review?slug=…`` is the hook that records the active
    slug (design-note resolution: slug-in-data-model, no broadcaster
    API change). The ``page:review`` renderer picks it up on the next
    tick.
    """
    from status_site.sources import review as review_page

    cfg, port = site
    status, body = req(port, "GET", f"/api/review?slug={SLUG}")
    assert status == 200

    payload = review_page.render(cfg)
    assert "a2ui" in payload
    envelopes = payload["a2ui"]
    assert [e["type"] for e in envelopes] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]
    assert all(e["surfaceId"] == "review" for e in envelopes)

    # The slug rides in the data model so a client clicking a review
    # button can POST /api/review-action with the right ``slug``.
    assert envelopes[0]["data"] == {"slug": SLUG}
    assert envelopes[-1]["path"] == "/slug"
    assert envelopes[-1]["value"] == SLUG

    # Three action buttons, each with its own textarea + semantic
    # ``action`` string the client switches on.
    comps = {c["id"]: c for c in envelopes[1]["components"]}
    for key in ("approve", "modify", "reject"):
        btn = comps[f"{key}-btn"]
        assert btn["type"] == "button"
        assert btn["props"]["action"] == f"review.{key}"
        assert btn["props"]["textareaId"] == f"{key}-ta"
        assert comps[f"{key}-ta"]["type"] == "textarea"

    # Plan body + title + meta are text components.
    assert comps["title"]["type"] == "text"
    assert "Review page for plans" in comps["title"]["props"]["markdown"]
    assert comps["plan"]["type"] == "text"
    assert "## Shape" in comps["plan"]["props"]["markdown"]

    # Clean up the module-level slug so subsequent tests start fresh.
    review_page.set_active_slug(None)


def test_review_a2ui_stays_empty_when_note_vanishes(site):
    """If the active slug points at a note that was deleted (or was
    never review-shaped), the renderer emits ``{"a2ui": []}`` rather
    than blowing up the SSE tick.
    """
    from status_site.sources import review as review_page

    cfg, _ = site
    review_page.set_active_slug("plain-note")  # exists but not review-shaped
    assert review_page.render(cfg) == {"a2ui": []}
    review_page.set_active_slug("missing")
    assert review_page.render(cfg) == {"a2ui": []}
    review_page.set_active_slug(None)
