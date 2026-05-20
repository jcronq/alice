"""Tests for the ``/designs`` route and the canvas-source ``.md`` skip.

Wires up:
- (a) ``GET /designs`` lists files in a tmp ``inner/designs/`` directory.
- (b) ``GET /designs/{slug}`` renders a known file's markdown body.
- (c) ``GET /canvas`` no longer lists ``*.md`` files dropped into
  ``inner/canvas/`` — per Jason's 2026-05-20 directive ("if there's a
  slide show, it's just a raw html file"), only ``*.html`` decks count.
"""

from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from alice_viewer.main import create_app
from alice_viewer.settings import Paths


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
    app = create_app(paths=paths)
    return paths, TestClient(app)


def test_designs_index_lists_files(tmp_path):
    paths, client = _bootstrap(tmp_path)
    ddir = paths.designs_dir
    ddir.mkdir(parents=True)
    (ddir / "2026-05-20-thinking-workflow-design.md").write_text(
        "# Thinking workflow\n\nA design draft about how thinking wakes.\n"
    )
    (ddir / "sleep-stages.md").write_text(
        "# Sleep stages\n\nSummary of the sleep-stage architecture.\n"
    )
    r = client.get("/designs")
    assert r.status_code == 200
    body = r.text
    assert "Thinking workflow" in body
    assert "Sleep stages" in body
    assert "/designs/2026-05-20-thinking-workflow-design" in body
    assert "/designs/sleep-stages" in body
    # Excerpts pulled from the first prose paragraph.
    assert "A design draft about how thinking wakes." in body


def test_designs_view_renders_known_file(tmp_path):
    paths, client = _bootstrap(tmp_path)
    ddir = paths.designs_dir
    ddir.mkdir(parents=True)
    (ddir / "decay-intervention-pitch.md").write_text(
        "---\nstatus: draft\n---\n"
        "# Decay intervention pitch\n\n"
        "Body paragraph with **bold** and a [link](http://x).\n"
    )
    r = client.get("/designs/decay-intervention-pitch")
    assert r.status_code == 200
    body = r.text
    assert "Decay intervention pitch" in body
    # Raw markdown body is embedded in the <pre id="design-raw"> hidden
    # block — marked.js renders client-side. Frontmatter is stripped.
    assert "Body paragraph with **bold**" in body
    assert "status: draft" not in body
    # Missing slug → 404.
    r404 = client.get("/designs/no-such-design")
    assert r404.status_code == 404


def test_canvas_index_no_longer_lists_md_files(tmp_path):
    """``/canvas`` only scans ``*.html`` under ``inner/canvas/`` now.
    ``*.md`` design drafts must move to ``/designs``."""
    paths, client = _bootstrap(tmp_path)
    (paths.inner / "canvas").mkdir(parents=True)
    (paths.inner / "canvas" / "stale-draft.md").write_text(
        "# Stale draft\n\nleftover markdown file.\n"
    )
    (paths.inner / "canvas" / "live-deck.html").write_text(
        "<html><head><title>Live deck</title></head>"
        "<body><h1>Live deck</h1></body></html>"
    )
    r = client.get("/canvas")
    assert r.status_code == 200
    body = r.text
    assert "Live deck" in body
    assert "/canvas/live-deck" in body
    assert "Stale draft" not in body
    assert "/canvas/stale-draft" not in body
    # The detail route also rejects the markdown slug.
    r_md = client.get("/canvas/stale-draft")
    assert r_md.status_code == 404
