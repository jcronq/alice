"""Tests for the Canvas dropdown nav (2026-05-20 nav restructure).

Per Jason's 2026-05-20 09:57 EDT directive, the flat ``canvas`` and
``designs`` top-level links collapse into a single ``Canvas ▾``
dropdown holding three sub-routes:

- ``/canvas``          — raw HTML slide decks (``inner/canvas/*.html``)
- ``/designs``         — markdown design drafts (``inner/designs/*.md``)
- ``/research-papers`` — experiment cards + flagged research notes

Each sub-route owns its index page. ``canvas_paper.html`` (the marked +
DOMPurify markdown render path) now lives behind ``/research-papers/{slug}``.
``/canvas/{slug}`` still works for HTML decks and 307-redirects to
``/research-papers/{slug}`` for any markdown slug, keeping
worker-generated links functional.
"""

from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from viewer.main import create_app
from viewer.settings import Paths


def _paths(tmp_path: pathlib.Path) -> Paths:
    return Paths(
        thinking_log=tmp_path / "thinking.log",
        speaking_log=tmp_path / "speaking.log",
        turn_log=tmp_path / "turns.jsonl",
        mind_dir=tmp_path / "mind",
        state_dir=tmp_path / "state",
    )


def _bootstrap(tmp_path: pathlib.Path) -> tuple[Paths, TestClient]:
    paths = _paths(tmp_path)
    paths.mind_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return paths, TestClient(create_app(paths=paths))


def _make_research(paths: Paths, slug: str, body: str) -> None:
    rdir = paths.mind_dir / "cortex-memory" / "research"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{slug}.md").write_text(body)


def _make_experiment(paths: Paths, slug: str, body: str) -> None:
    edir = paths.mind_dir / "cortex-memory" / "experiments"
    edir.mkdir(parents=True, exist_ok=True)
    (edir / f"{slug}.md").write_text(body)


# ---------------------------------------------------------------------------
# (a) /research-papers index lists notes with canvas_paper: true


def test_research_papers_index_lists_flagged_notes(tmp_path):
    paths, client = _bootstrap(tmp_path)
    _make_research(
        paths,
        "2026-05-20-rp-alpha",
        "---\ncanvas_paper: true\n---\n# Alpha Paper\nbody\n",
    )
    _make_research(
        paths,
        "2026-05-20-rp-omega",
        "---\ncanvas_paper: yes\n---\n# Omega Paper\nbody\n",
    )
    _make_experiment(
        paths,
        "exp-card-001",
        "# Experiment One\nresults\n",
    )
    # Unflagged research notes should NOT show up.
    _make_research(
        paths, "2026-05-20-rp-private", "---\ntitle: Hidden\n---\n# Hidden\n"
    )
    r = client.get("/research-papers")
    assert r.status_code == 200
    body = r.text
    assert "Alpha Paper" in body
    assert "Omega Paper" in body
    assert "Experiment One" in body
    assert "/research-papers/2026-05-20-rp-alpha" in body
    assert "/research-papers/exp-card-001" in body
    # Unflagged research stays hidden.
    assert "Hidden" not in body


# ---------------------------------------------------------------------------
# (b) /research-papers/{slug} detail renders via canvas_paper.html


def test_research_papers_detail_renders_flagged_note(tmp_path):
    paths, client = _bootstrap(tmp_path)
    _make_research(
        paths,
        "2026-05-20-render-test",
        "---\ncanvas_paper: true\n---\n# Rendered Paper\nbody paragraph\n",
    )
    r = client.get("/research-papers/2026-05-20-render-test")
    assert r.status_code == 200
    body = r.text
    # canvas_paper.html embeds the raw markdown in <pre id="paper-raw">
    # so marked.js can render it client-side. We assert against the
    # server-side payload directly.
    assert "Rendered Paper" in body
    assert "body paragraph" in body
    assert 'id="paper-raw"' in body
    # No fallback banner for an opt-in flagged paper.
    assert "rendering as plain markdown" not in body


def test_research_papers_detail_renders_experiment_card(tmp_path):
    paths, client = _bootstrap(tmp_path)
    _make_experiment(paths, "exp-detail-001", "# Exp Detail\nfindings\n")
    r = client.get("/research-papers/exp-detail-001")
    assert r.status_code == 200
    assert "Exp Detail" in r.text
    assert "findings" in r.text


def test_research_papers_detail_unflagged_fallback_banner(tmp_path):
    """Issue #175 fallback — an unflagged research note still resolves
    on /research-papers/{slug} with a banner explaining the format."""
    paths, client = _bootstrap(tmp_path)
    _make_research(
        paths,
        "2026-05-20-unflagged",
        "---\ntitle: U\n---\n# Unflagged Note\nplain body\n",
    )
    r = client.get("/research-papers/2026-05-20-unflagged")
    assert r.status_code == 200
    body = r.text
    assert "Unflagged Note" in body
    assert "rendering as plain markdown" in body


def test_canvas_slug_redirects_markdown_to_research_papers(tmp_path):
    """``/canvas/{slug}`` 307-redirects to ``/research-papers/{slug}``
    for any markdown content, preserving worker-generated links."""
    paths, client = _bootstrap(tmp_path)
    _make_research(
        paths,
        "2026-05-20-redirect-me",
        "---\ncanvas_paper: true\n---\n# Redirect Me\nx\n",
    )
    # TestClient follows redirects by default; disable to inspect the hop.
    r = client.get("/canvas/2026-05-20-redirect-me", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/research-papers/2026-05-20-redirect-me"


# ---------------------------------------------------------------------------
# (c) Nav chrome renders the Canvas dropdown with all three sub-items


def test_nav_renders_canvas_dropdown_with_three_items(tmp_path):
    """Any page that extends base.html (e.g. /canvas) should ship the
    Canvas dropdown chrome with its three sub-links."""
    _paths_, client = _bootstrap(tmp_path)
    r = client.get("/canvas")
    assert r.status_code == 200
    body = r.text
    # The dropdown summary plus its three children.
    assert "<summary>canvas" in body
    assert 'href="/canvas"' in body
    assert 'href="/designs"' in body
    assert 'href="/research-papers"' in body
    # Old flat link wording is gone.
    assert '<a href="/canvas"     class=' not in body  # flat anchor removed


# ---------------------------------------------------------------------------
# (d) active highlight applies to the correct sub-route


def test_canvas_active_state_on_canvas_route(tmp_path):
    _paths_, client = _bootstrap(tmp_path)
    r = client.get("/canvas")
    body = r.text
    # Parent dropdown is .on; the canvas sub-link is .on; siblings aren't.
    assert "nav-dropdown on" in body or 'class="nav-dropdown on' in body
    # The slide-deck child link is active.
    assert 'href="/canvas"          class="on"' in body
    # The other two children are not.
    assert 'href="/designs"         class=""' in body
    assert 'href="/research-papers" class=""' in body


def test_canvas_active_state_on_designs_route(tmp_path):
    paths, client = _bootstrap(tmp_path)
    paths.designs_dir.mkdir(parents=True, exist_ok=True)
    r = client.get("/designs")
    body = r.text
    assert 'href="/canvas"          class=""' in body
    assert 'href="/designs"         class="on"' in body
    assert 'href="/research-papers" class=""' in body


def test_canvas_active_state_on_research_papers_route(tmp_path):
    _paths_, client = _bootstrap(tmp_path)
    r = client.get("/research-papers")
    body = r.text
    assert 'href="/canvas"          class=""' in body
    assert 'href="/designs"         class=""' in body
    assert 'href="/research-papers" class="on"' in body


# ---------------------------------------------------------------------------
# /canvas empty-state copy points the user at the right dir


def test_canvas_index_empty_state_has_directive_copy(tmp_path):
    """When no HTML files live under ``inner/canvas/``, the page tells
    the user where to drop them."""
    paths, client = _bootstrap(tmp_path)
    (paths.inner / "canvas").mkdir(parents=True, exist_ok=True)
    r = client.get("/canvas")
    assert r.status_code == 200
    body = r.text
    assert "No HTML slide decks yet" in body
    assert "alice-mind/inner/canvas/" in body
