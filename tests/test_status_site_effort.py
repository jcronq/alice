"""Slice-effort tests: 4-segment effort picker + persistence + Alice-input header.

Covers the pieces status-site.py grew in Slice-effort:

- ``read_woy_effort`` is append-only JSONL, last write wins per slug,
  and treats out-of-band levels as an explicit clear.
- ``_append_woy_effort`` writes a schema-valid JSONL line that
  ``read_woy_effort`` can round-trip.
- ``handle_effort_action`` validates key / slug / level the same way
  ``handle_woy_action`` does, and the empty / null level is a legal
  clear verb.
- ``render_surface`` prepends ``effort=<level>`` as the first body
  line when the caller passes a level, and omits the line entirely
  when the level is unset (so Alice's own guess stays active).
- ``handle_reply`` picks up the latest effort for the item's thread
  id and threads it into the surface — the full round-trip Jason
  cares about ("I picked Solid → next time Alice sees this item,
  her input starts with ``effort=Solid``").
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from status_site import server as S


# ---------------------------------------------------------------- helpers


def _mk_cfg(tmp_path: Path) -> S.Config:
    """Minimal Config duck-type — mirrors the fixture in
    ``test_status_site_woy_actions.py`` so slice-effort tests can
    share the key and lock conventions without cross-importing.
    """
    d = tmp_path / "status"
    d.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    lock = threading.Lock()

    class _C:
        status_dir = d
        threads_dir = d / "threads"
        surface_dir = surface
        reply_log = d / ".replies.jsonl"
        key = "k"
        public_url = "http://localhost:8095"
        max_body = 10_000
        max_upload = 1_000_000  # ingest_attachments reads this up front.
    cfg = _C()
    cfg.lock = lock
    return cfg


# ---------------------------------------------------------------- read_woy_effort


def test_read_woy_effort_missing_file_returns_empty(tmp_path):
    assert S.read_woy_effort(tmp_path) == {}


def test_read_woy_effort_last_write_wins_per_slug(tmp_path):
    p = tmp_path / S.WOY_EFFORT_FILE
    p.write_text(
        '\n'.join([
            json.dumps({"slug": "item-one", "level": "spike", "ts": "2026-01-01T00:00:00Z"}),
            json.dumps({"slug": "item-one", "level": "solid", "ts": "2026-01-02T00:00:00Z"}),
            json.dumps({"slug": "item-two", "level": "autonomous", "ts": "2026-01-03T00:00:00Z"}),
        ]) + "\n"
    )
    assert S.read_woy_effort(tmp_path) == {
        "item-one": "solid",
        "item-two": "autonomous",
    }


def test_read_woy_effort_empty_level_clears_slug(tmp_path):
    """A later record with ``level=""`` drops the slug back to unset,
    so Alice can resume her own guess once Jason un-picks."""
    p = tmp_path / S.WOY_EFFORT_FILE
    p.write_text(
        '\n'.join([
            json.dumps({"slug": "a", "level": "prototype"}),
            json.dumps({"slug": "a", "level": ""}),
        ]) + "\n"
    )
    assert S.read_woy_effort(tmp_path) == {}


def test_read_woy_effort_unknown_level_treated_as_clear(tmp_path):
    p = tmp_path / S.WOY_EFFORT_FILE
    p.write_text(
        '\n'.join([
            json.dumps({"slug": "b", "level": "solid"}),
            json.dumps({"slug": "b", "level": "mega"}),
        ]) + "\n"
    )
    # Out-of-band levels should NOT persist — they drop the prior
    # pick instead of silently keeping it, so an attacker-controlled
    # writer can't pin a stale level by emitting garbage after.
    assert S.read_woy_effort(tmp_path) == {}


def test_read_woy_effort_skips_malformed_lines(tmp_path):
    p = tmp_path / S.WOY_EFFORT_FILE
    p.write_text(
        '\n'.join([
            "{not json",
            json.dumps({"slug": "c", "level": "autonomous"}),
            json.dumps(["not", "an", "object"]),
        ]) + "\n"
    )
    assert S.read_woy_effort(tmp_path) == {"c": "autonomous"}


def test_append_woy_effort_round_trips(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    S._append_woy_effort(d, slug="round-trip", level="prototype")
    S._append_woy_effort(d, slug="round-trip", level="solid")
    assert S.read_woy_effort(d) == {"round-trip": "solid"}


def test_append_woy_effort_empty_level_appends_clear(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    S._append_woy_effort(d, slug="x", level="solid")
    S._append_woy_effort(d, slug="x", level="")
    assert S.read_woy_effort(d) == {}


# ---------------------------------------------------------------- handle_effort_action


def test_handle_effort_action_bad_key_raises_403(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_effort_action(cfg, {"key": "nope", "slug": "x", "level": "spike"})
    assert e.value.code == 403


def test_handle_effort_action_bad_slug_raises_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_effort_action(cfg, {"key": "k", "slug": "", "level": "spike"})
    assert e.value.code == 400


def test_handle_effort_action_bad_level_raises_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_effort_action(cfg, {"key": "k", "slug": "x", "level": "mega"})
    assert e.value.code == 400


@pytest.mark.parametrize("level", S.WOY_EFFORT_LEVELS)
def test_handle_effort_action_persists_each_valid_level(tmp_path, level):
    cfg = _mk_cfg(tmp_path)
    resp = S.handle_effort_action(cfg, {"key": "k", "slug": "pick", "level": level})
    assert resp == {"ok": True, "slug": "pick", "level": level}
    assert S.read_woy_effort(cfg.status_dir) == {"pick": level}


def test_handle_effort_action_clears_on_empty_level(tmp_path):
    cfg = _mk_cfg(tmp_path)
    S.handle_effort_action(cfg, {"key": "k", "slug": "p", "level": "solid"})
    assert S.read_woy_effort(cfg.status_dir) == {"p": "solid"}
    resp = S.handle_effort_action(cfg, {"key": "k", "slug": "p", "level": ""})
    assert resp == {"ok": True, "slug": "p", "level": ""}
    assert S.read_woy_effort(cfg.status_dir) == {}


def test_handle_effort_action_clears_on_null_level(tmp_path):
    cfg = _mk_cfg(tmp_path)
    S.handle_effort_action(cfg, {"key": "k", "slug": "p", "level": "solid"})
    resp = S.handle_effort_action(cfg, {"key": "k", "slug": "p", "level": None})
    assert resp == {"ok": True, "slug": "p", "level": ""}
    assert S.read_woy_effort(cfg.status_dir) == {}


def test_handle_effort_action_normalises_case_and_slug(tmp_path):
    """Jason's picks come straight from the UI's EFFORT_SEGMENTS, but
    the endpoint is still the HTTP boundary — accept mixed case and
    lowercase it, and run slugs through ``safe_slug`` so a user who
    typed a URL into the thread field can't land /api/effort records
    under a weird key."""
    cfg = _mk_cfg(tmp_path)
    resp = S.handle_effort_action(cfg, {"key": "k", "slug": "Review-PR-99", "level": "SOLID"})
    assert resp["level"] == "solid"
    assert resp["slug"] == "review-pr-99"
    assert S.read_woy_effort(cfg.status_dir) == {"review-pr-99": "solid"}


# ---------------------------------------------------------------- render_surface


def test_render_surface_prepends_effort_line_when_set(tmp_path):
    cfg = _mk_cfg(tmp_path)
    doc = S.render_surface(
        cfg, item="Review PR 99", tid="review-pr-99", page="index",
        text="looks good", prior=[], files=[], effort="solid",
    )
    # Must land in the body section (not the YAML frontmatter), and
    # must be the first non-empty body line so Alice sees the effort
    # before any other content.
    body = doc.split("---\n", 2)[-1]
    first_line = body.lstrip().splitlines()[0]
    assert first_line == "effort=solid"


def test_render_surface_omits_effort_line_when_unset(tmp_path):
    cfg = _mk_cfg(tmp_path)
    doc = S.render_surface(
        cfg, item="Review PR 99", tid="review-pr-99", page="index",
        text="looks good", prior=[], files=[], effort=None,
    )
    # The literal substring is safer than parsing — a stray header
    # line would show up even if the rest of the doc changed.
    assert "effort=" not in doc


def test_render_surface_omits_effort_line_when_blank(tmp_path):
    """Empty-string is the clear verb at the storage layer; the
    renderer must treat it the same as unset (no header line)."""
    cfg = _mk_cfg(tmp_path)
    doc = S.render_surface(
        cfg, item="x", tid="x", page="index",
        text="t", prior=[], files=[], effort="",
    )
    assert "effort=" not in doc


# ---------------------------------------------------------------- integration: pick → reply → surface


def test_reply_surface_carries_effort_when_picked_first(tmp_path):
    """End-to-end: Jason picks Solid, then replies on the item.
    The resulting surface file (what Alice actually reads) must
    begin its body with ``effort=solid``."""
    cfg = _mk_cfg(tmp_path)
    (cfg.status_dir / "index.md").write_text(
        "## Waiting on you\n- [ ] **Decide X** some body\n"
    )
    # Jason picks the level.
    S.handle_effort_action(cfg, {"key": "k", "slug": "decide-x", "level": "solid"})
    # Then replies through the same ExpandedCard the picker lives in.
    resp = S.handle_reply(cfg, {
        "key": "k",
        "item": "Decide X",
        "thread": "decide-x",
        "page": "index",
        "text": "go with option A",
    })
    assert resp["ok"] is True
    # Exactly one surface file dropped; its body begins with the
    # effort header line.
    surfaces = list(cfg.surface_dir.glob("*.md"))
    assert len(surfaces) == 1
    body = surfaces[0].read_text().split("---\n", 2)[-1].lstrip()
    assert body.splitlines()[0] == "effort=solid"


def test_reply_surface_omits_effort_when_not_picked(tmp_path):
    """Alice's own guess stays active when Jason hasn't touched the
    picker. The surface must contain no ``effort=`` header line."""
    cfg = _mk_cfg(tmp_path)
    (cfg.status_dir / "index.md").write_text(
        "## Waiting on you\n- [ ] **Decide Y** some body\n"
    )
    resp = S.handle_reply(cfg, {
        "key": "k",
        "item": "Decide Y",
        "thread": "decide-y",
        "page": "index",
        "text": "waiting on data",
    })
    assert resp["ok"] is True
    surfaces = list(cfg.surface_dir.glob("*.md"))
    assert len(surfaces) == 1
    assert "effort=" not in surfaces[0].read_text()


def test_reply_surface_uses_latest_effort_after_rewire(tmp_path):
    """Jason picks Spike, picks Solid, then replies — the surface
    must reflect Solid, matching the last-write-wins read semantics."""
    cfg = _mk_cfg(tmp_path)
    (cfg.status_dir / "index.md").write_text(
        "## Waiting on you\n- [ ] **Decide Z** body\n"
    )
    S.handle_effort_action(cfg, {"key": "k", "slug": "decide-z", "level": "spike"})
    S.handle_effort_action(cfg, {"key": "k", "slug": "decide-z", "level": "solid"})
    S.handle_reply(cfg, {
        "key": "k",
        "item": "Decide Z",
        "thread": "decide-z",
        "page": "index",
        "text": "pick me",
    })
    surfaces = list(cfg.surface_dir.glob("*.md"))
    assert len(surfaces) == 1
    body = surfaces[0].read_text().split("---\n", 2)[-1].lstrip()
    assert body.splitlines()[0] == "effort=solid"
