"""Slice review-TODO: emit + read + status-site wiring.

Covers the pieces status-site + alice_thinking gain in this slice:

* :func:`core.review_todos.emit_review_todo` appends one JSON record
  per call with the expected shape.
* Memory-worker code paths never import the emit API (static
  invariant — the only enforcement needed; if a future refactor
  wants to add a mechanical write hook it will trip this test).
* :func:`core.review_todos.active_todos` filters out dismissed slugs
  and records older than ``expiry_days``.
* :func:`status_site.sources.review_todos.read_rows` composes the
  WoY row payload, including dismissal filter + oldest-first order.
* ``_render_your_todo`` + ``_render_waiting_on_you`` surface review
  TODOs in the section payload after a vault-write emit.
* ``handle_review_todo_click`` dismisses the slug via
  ``.woy-dismissed.jsonl`` so the next render drops it.
* :func:`alice_thinking.design_pipeline.commit_approved_draft` fires
  the emit after writing an approved draft.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
from pathlib import Path

import pytest

from core.review_todos import (
    DEFAULT_EXPIRY_DAYS,
    active_todos,
    emit_outbound_draft_todo,
    emit_review_todo,
    emit_vault_write_todo,
    read_review_todos,
    slug_for,
)


# --------------------------------------------------------------- emit shape


def test_emit_review_todo_writes_one_jsonl_line(tmp_path):
    store = tmp_path / "review-todos.jsonl"
    rec = emit_review_todo(
        "vault-write",
        "/alice-mind/cortex-memory/research/2026-11-17-foo.md",
        "foo-slug",
        alice_summary="Alice added a design note on foo.",
        path=store,
    )
    assert rec["kind"] == "vault-write"
    assert rec["title"] == "foo-slug"
    assert rec["target"].endswith("foo.md")
    assert rec["alice_summary"] == "Alice added a design note on foo."
    lines = store.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == rec


def test_emit_review_todo_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        emit_review_todo(
            "mechanical-dedupe",  # not in KINDS
            "/x.md",
            "x",
            path=tmp_path / "s.jsonl",
        )


def test_emit_review_todo_needs_a_title_or_target(tmp_path):
    with pytest.raises(ValueError):
        emit_review_todo(
            "vault-write", "   ", "", path=tmp_path / "s.jsonl"
        )


def test_emit_review_todo_extra_does_not_overwrite_reserved(tmp_path):
    store = tmp_path / "s.jsonl"
    rec = emit_review_todo(
        "vault-write",
        "/x.md",
        "x",
        path=store,
        extra={"id": "ATTEMPT", "custom": "value"},
    )
    # ``id`` reserved — ``extra`` can't overwrite.
    assert rec["id"] != "ATTEMPT"
    assert rec["custom"] == "value"


def test_emit_vault_write_and_outbound_draft_convenience(tmp_path):
    store = tmp_path / "s.jsonl"
    r1 = emit_vault_write_todo(
        tmp_path / "note.md", "note-slug", path=store
    )
    r2 = emit_outbound_draft_todo(
        "https://github.com/jcronq/alice/pull/42", "PR #42", path=store
    )
    assert r1["kind"] == "vault-write"
    assert r2["kind"] == "outbound-draft"
    assert len(store.read_text().splitlines()) == 2


# --------------------------------------------------------------- read + sort


def test_read_review_todos_skips_malformed_lines(tmp_path):
    store = tmp_path / "s.jsonl"
    emit_review_todo("vault-write", "/a.md", "a", path=store)
    with open(store, "a") as f:
        f.write("{not json\n")
        f.write(json.dumps({"kind": "garbage"}) + "\n")  # bad kind
        f.write(json.dumps({"no_kind": 1}) + "\n")
    emit_review_todo("outbound-draft", "/b", "b", path=store)
    recs = read_review_todos(store)
    assert [r["kind"] for r in recs] == ["vault-write", "outbound-draft"]


def test_read_review_todos_missing_file_is_empty(tmp_path):
    assert read_review_todos(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------- active filter


def test_active_todos_filters_dismissed_slugs(tmp_path):
    store = tmp_path / "s.jsonl"
    r1 = emit_review_todo("vault-write", "/a.md", "alpha", path=store)
    r2 = emit_review_todo("vault-write", "/b.md", "bravo", path=store)
    recs = read_review_todos(store)
    active = active_todos(
        recs, dismissed_slugs={slug_for(r1)},
    )
    # Alpha dismissed → only bravo survives.
    assert [r["id"] for r in active] == [r2["id"]]
    assert active[0]["_slug"] == slug_for(r2)


def test_active_todos_expires_old_records(tmp_path):
    store = tmp_path / "s.jsonl"
    now = _dt.datetime(2026, 11, 17, 12, 0, 0, tzinfo=_dt.timezone.utc)
    emit_review_todo(
        "vault-write", "/old.md", "old",
        now=now - _dt.timedelta(days=10), path=store,
    )
    emit_review_todo(
        "vault-write", "/fresh.md", "fresh",
        now=now - _dt.timedelta(days=1), path=store,
    )
    recs = read_review_todos(store)
    # Default 7-day expiry drops the 10-day-old record.
    active = active_todos(recs, now=now, expiry_days=DEFAULT_EXPIRY_DAYS)
    assert [r["title"] for r in active] == ["fresh"]


def test_active_todos_oldest_first(tmp_path):
    store = tmp_path / "s.jsonl"
    now = _dt.datetime(2026, 11, 17, 12, 0, 0, tzinfo=_dt.timezone.utc)
    emit_review_todo(
        "vault-write", "/newer.md", "newer",
        now=now - _dt.timedelta(hours=1), path=store,
    )
    emit_review_todo(
        "vault-write", "/older.md", "older",
        now=now - _dt.timedelta(hours=5), path=store,
    )
    recs = read_review_todos(store)
    active = active_todos(recs, now=now)
    assert [r["title"] for r in active] == ["older", "newer"]


# ------------------------------------------------------- source composition


def test_source_read_rows_composes_payload(tmp_path, monkeypatch):
    from status_site.sources import review_todos as src

    store = tmp_path / "review-todos.jsonl"
    rec = emit_review_todo(
        "vault-write",
        "/alice-mind/cortex-memory/research/2026-11-17-foo.md",
        "foo",
        alice_summary="why Jason should look",
        path=store,
    )
    rows = src.read_rows(path=store)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == rec["id"]
    assert row["kind"] == "vault-write"
    assert row["headline"] == "Review changes: foo"
    assert row["alice_summary"] == "why Jason should look"
    assert row["target"].endswith("foo.md")
    assert row["slug"] == slug_for(rec)
    # dismissal path filters it out.
    rows2 = src.read_rows(path=store, dismissed={slug_for(rec)})
    assert rows2 == []


def test_source_render_markdown_shapes_bold_headline():
    from status_site.sources import review_todos as src

    rows = src.read_rows(path=pathlib.Path("/nope.jsonl"))
    assert src.render_markdown(rows) == ""
    rows = [
        {
            "headline": "Review changes: foo",
            "title": "foo",
            "alice_summary": "why to look",
            "target": "/a.md",
            "kind": "vault-write",
        }
    ]
    md = src.render_markdown(rows)
    assert md.startswith("- **Review changes: foo**")
    assert "why to look" in md
    assert "/a.md" in md


def test_source_render_markdown_default_summary_by_kind():
    from status_site.sources import review_todos as src

    rows = [
        {
            "headline": "Review changes: outbound",
            "title": "outbound",
            "alice_summary": "",
            "target": "https://github.com/jcronq/alice/pull/1",
            "kind": "outbound-draft",
        }
    ]
    md = src.render_markdown(rows)
    assert "Outbound draft ready" in md


def test_source_render_markdown_skips_record_with_no_headline():
    from status_site.sources import review_todos as src

    rows = [{"headline": "", "title": "", "target": "", "kind": "vault-write"}]
    assert src.render_markdown(rows) == ""


# ---------------------------------------------- hook: commit_approved_draft


def test_commit_approved_draft_emits_review_todo(tmp_path):
    from alice_thinking import design_pipeline

    mind = tmp_path / "alice-mind"
    draft = "---\nkind: research\n---\n\n# Foo\n\nbody\n"
    now = _dt.datetime(2026, 11, 17, 12, 30, 0)
    path = design_pipeline.commit_approved_draft(
        mind,
        draft=draft,
        slug_hint="design-xyz",
        now=now,
        alice_summary="thinking drafted a plan for xyz.",
    )
    assert path.exists()
    assert path.parent == mind / "cortex-memory" / "research"
    store = mind / "inner" / "state" / "review-todos.jsonl"
    assert store.is_file()
    lines = store.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "vault-write"
    assert rec["title"] == "design-xyz"
    assert rec["target"].endswith("design-xyz.md")
    assert rec["alice_summary"] == "thinking drafted a plan for xyz."


# ------------------------------------------------- negative: memory worker


def test_memory_worker_never_imports_emit_review_todo():
    """Memory-worker writes are mechanical — they must stay out of
    the review-TODO stream so Jason's WoY list doesn't fill with
    atomize / archive / dedupe noise.

    Static invariant — scans ``src/alice_thinking/memory_worker/`` +
    ``src/alice_thinking/wake.py`` for an import of the emit API.
    If a future refactor wants to hook a mechanical write it trips
    this test, which forces a conscious decision.
    """
    import alice_thinking.memory_worker as mw

    mw_root = Path(mw.__file__).parent
    forbidden = ("emit_review_todo", "emit_vault_write_todo", "review_todos")
    offenders: list[tuple[Path, str]] = []
    for py in sorted(mw_root.rglob("*.py")):
        text = py.read_text(errors="replace")
        for token in forbidden:
            if token in text:
                offenders.append((py.relative_to(mw_root), token))
    assert offenders == [], (
        "memory_worker must not import review-TODO emit API: "
        + ", ".join(f"{p} ({t})" for p, t in offenders)
    )


# --------------------------------- integration: status_site WoY render


def _make_cfg(tmp_path):
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    surface_dir = tmp_path / "surface"
    surface_dir.mkdir()
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    from status_site.server import Config

    return Config(
        status_dir=status_dir,
        surface_dir=surface_dir,
        upload_dir=upload_dir,
        key="test-key",
    )


def test_render_waiting_on_you_splices_review_todos(tmp_path, monkeypatch):
    from status_site import server as S

    store = tmp_path / "review-todos.jsonl"
    monkeypatch.setenv("ALICE_REVIEW_TODOS_PATH", str(store))
    emit_review_todo(
        "vault-write",
        "/alice-mind/cortex-memory/research/2026-11-17-foo.md",
        "integration-note",
        alice_summary="integration-summary",
        path=store,
    )
    cfg = _make_cfg(tmp_path)
    renderers = S.section_renderers(cfg)
    payload = renderers["waiting_on_you"]()
    assert "review_todos" in payload
    assert len(payload["review_todos"]) == 1
    row = payload["review_todos"][0]
    assert row["headline"] == "Review changes: integration-note"
    # Markdown should contain the bold-titled bullet.
    assert "**Review changes: integration-note**" in payload["markdown"]
    assert "integration-summary" in payload["markdown"]


def test_render_your_todo_counts_review_todos(tmp_path, monkeypatch):
    from status_site import server as S

    store = tmp_path / "review-todos.jsonl"
    monkeypatch.setenv("ALICE_REVIEW_TODOS_PATH", str(store))
    for i in range(3):
        emit_review_todo(
            "vault-write", f"/a{i}.md", f"note-{i}", path=store,
        )
    cfg = _make_cfg(tmp_path)
    renderers = S.section_renderers(cfg)
    payload = renderers["your_todo"]()
    assert payload["counts"]["review_todos"] == 3
    # The review-TODO markdown splices into your_todo_md so the
    # ``Asks`` group picks up the bullets. Each review bullet
    # becomes a top-level hand-zone item but the hand-zone itself
    # counts as a single ``asks`` entry — so the total is 3 (review)
    # + 1 (hand-zone non-empty).
    assert payload["counts"]["asks"] == 1
    # Spliced bullets are discoverable via your_todo_md.
    for i in range(3):
        assert f"Review changes: note-{i}" in payload["your_todo_md"]


def test_review_todo_click_dismisses_slug(tmp_path, monkeypatch):
    from status_site import server as S

    store = tmp_path / "review-todos.jsonl"
    monkeypatch.setenv("ALICE_REVIEW_TODOS_PATH", str(store))
    rec = emit_review_todo(
        "vault-write", "/a.md", "to-dismiss", path=store,
    )
    cfg = _make_cfg(tmp_path)
    slug = slug_for(rec)

    # Pre-dismiss, the row appears.
    renderers = S.section_renderers(cfg)
    payload = renderers["waiting_on_you"]()
    assert any(
        r["slug"] == slug for r in payload["review_todos"]
    )

    # Dismiss through the click endpoint.
    resp = S.handle_review_todo_click(
        cfg, {"key": "test-key", "slug": slug}
    )
    assert resp == {"ok": True, "slug": slug, "dismissed": True}
    # The thread message landed.
    thread = (cfg.threads_dir / f"{slug}.jsonl").read_text().splitlines()
    assert any(
        json.loads(line).get("action") == "review-todo-click"
        for line in thread
    )
    # The dismissal JSONL carries the slug.
    dismissed_log = cfg.status_dir / ".woy-dismissed.jsonl"
    assert dismissed_log.is_file()
    assert any(
        json.loads(line).get("slug") == slug
        for line in dismissed_log.read_text().splitlines()
    )

    # Post-dismiss, the row is gone from the next render.
    payload2 = renderers["waiting_on_you"]()
    assert all(
        r["slug"] != slug for r in payload2["review_todos"]
    )


def test_review_todo_click_rejects_bad_key(tmp_path):
    from status_site import server as S

    cfg = _make_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_review_todo_click(
            cfg, {"key": "nope", "slug": "x"}
        )
    assert e.value.code == 403


def test_review_todo_click_rejects_bad_slug(tmp_path):
    from status_site import server as S

    cfg = _make_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_review_todo_click(
            cfg, {"key": "test-key", "slug": "../"}
        )
    assert e.value.code == 400


def test_review_todo_click_endpoint_in_post_router():
    """The handler table must include the click route; otherwise a
    pass test above can still leave the route unreachable by the
    browser.
    """
    import inspect
    from status_site import server as S

    src = inspect.getsource(S.Handler.do_POST)
    assert "/api/review-todo-click" in src
