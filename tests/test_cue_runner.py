"""Tests for the speaking-side cue runner.

Coverage:
- Tokenizer + FTS-MATCH builder
- Type-aware boost classifier (``classify_note``)
- Matched-line extractor
- FTS query path against a temp sqlite DB seeded with a tiny vault
- Trigger-keyword extra boost (frontmatter parsing)
- Packet formatter
- Dedup against ``context_slugs``
- Top-level fail-soft: missing DB returns ``""``
- ``enabled=False`` short-circuit
- access_count bump (sync helper, no event loop)
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any

import pytest

from alice_speaking.retrieval import cue_runner
from alice_speaking.retrieval.cue_runner import (
    BEHAVIOR_BOOST,
    BUCKET1_BOOST,
    BUCKET2_BOOST,
    STATE_BOOST,
    _bump_access,
    _build_fts_match,
    _Candidate,
    _format_packet,
    _read_trigger_keywords,
    _tokenize_query,
    build_cue_packet,
    classify_note,
    extract_matched_lines,
)


# ---------------------------------------------------------------------------
# classify_note


def test_classify_state_types_get_state_boost():
    assert classify_note("daily", []) == STATE_BOOST
    assert classify_note("state-snapshot", []) == STATE_BOOST
    assert classify_note("skill", []) == STATE_BOOST


def test_classify_behavior_gets_behavior_boost():
    assert classify_note("behavior", []) == BEHAVIOR_BOOST


def test_classify_finding_or_bucket2_tag_gets_bucket2_boost():
    assert classify_note("finding", []) == BUCKET2_BOOST
    assert classify_note("research", ["cozyhem"]) == BUCKET2_BOOST
    assert classify_note("research", ["alice-architecture"]) == BUCKET2_BOOST


def test_classify_default_is_bucket1():
    assert classify_note("research", ["random-tag"]) == BUCKET1_BOOST
    assert classify_note("", []) == BUCKET1_BOOST


# ---------------------------------------------------------------------------
# _tokenize_query


def test_tokenize_drops_stopwords_and_short_tokens():
    assert _tokenize_query("what is the latest on cozyhem?") == ["latest", "cozyhem"]


def test_tokenize_dedupes_preserving_order():
    assert _tokenize_query("cozyhem cozyhem fitness cozyhem") == ["cozyhem", "fitness"]


def test_tokenize_empty_when_only_stopwords():
    # All words below are in the stopword list; result must be empty.
    assert _tokenize_query("the of to and") == []


# ---------------------------------------------------------------------------
# _build_fts_match


def test_build_fts_match_quotes_and_ors():
    assert _build_fts_match(["foo", "bar"]) == '"foo" OR "bar"'


def test_build_fts_match_strips_inner_quotes():
    assert _build_fts_match(['foo"x']) == '"foox"'


# ---------------------------------------------------------------------------
# extract_matched_lines


def test_extract_matched_lines_returns_line_numbers_and_text():
    body = "intro line\ncozyhem reference here\nunrelated\nmore cozyhem stuff"
    out = extract_matched_lines(body, ["cozyhem"], max_n=5)
    assert out == [
        {"n": 2, "text": "cozyhem reference here"},
        {"n": 4, "text": "more cozyhem stuff"},
    ]


def test_extract_matched_lines_caps_at_max_n():
    body = "\n".join(f"cozyhem line {i}" for i in range(10))
    out = extract_matched_lines(body, ["cozyhem"], max_n=3)
    assert len(out) == 3


def test_extract_matched_lines_skips_short_lines():
    body = "ok\ncozyhem ok long\nx"
    out = extract_matched_lines(body, ["cozyhem"])
    assert out == [{"n": 2, "text": "cozyhem ok long"}]


def test_extract_matched_lines_no_terms_returns_empty():
    assert extract_matched_lines("anything", []) == []


# ---------------------------------------------------------------------------
# Trigger-keywords frontmatter


def test_read_trigger_keywords_parses_inline_list(tmp_path: pathlib.Path):
    note = tmp_path / "research" / "foo.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: Foo\ntrigger_keywords: [cozyhem, lights]\n---\nbody\n")
    assert _read_trigger_keywords(tmp_path, "research/foo.md") == ["cozyhem", "lights"]


def test_read_trigger_keywords_missing_field_returns_empty(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text("---\ntitle: A\n---\nbody\n")
    assert _read_trigger_keywords(tmp_path, "a.md") == []


def test_read_trigger_keywords_no_frontmatter_returns_empty(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text("just a body\n")
    assert _read_trigger_keywords(tmp_path, "a.md") == []


def test_read_trigger_keywords_missing_file_returns_empty(tmp_path: pathlib.Path):
    assert _read_trigger_keywords(tmp_path, "nope.md") == []


# ---------------------------------------------------------------------------
# Packet formatter


def _candidate(
    slug: str, *, title: str | None = None, lines=None, why=""
) -> _Candidate:
    return _Candidate(
        slug=slug,
        title=title or slug,
        note_type="",
        tags=[],
        body="",
        path=f"{slug}.md",
        fts_rank=-1.0,
        boost=1.0,
        final_score=1.0,
        matched_lines=lines or [{"n": 1, "text": "matched line"}],
        why_relevant=why,
    )


def test_format_packet_renders_titles_and_lines():
    out = _format_packet(
        [
            _candidate(
                "foo",
                title="Foo Title",
                lines=[
                    {"n": 5, "text": "first match"},
                    {"n": 12, "text": "second match"},
                ],
            ),
            _candidate("bar", title="Bar Title"),
        ],
        packet_token_ceiling=1000,
    )
    assert "[VAULT CONTEXT" in out
    assert "1. **Foo Title** (`foo`)" in out
    assert "> L5: first match" in out
    assert "> L12: second match" in out
    assert "2. **Bar Title** (`bar`)" in out
    assert "[End vault context]" in out


def test_format_packet_includes_why_relevant_when_set():
    out = _format_packet(
        [_candidate("foo", why="direct hit on the keyword")],
        packet_token_ceiling=1000,
    )
    assert "_Why relevant: direct hit on the keyword_" in out


def test_format_packet_empty_returns_empty_string():
    assert _format_packet([], packet_token_ceiling=1000) == ""


def test_format_packet_token_ceiling_caps_entries():
    big_lines = [{"n": i, "text": "x" * 200} for i in range(1, 6)]
    cands = [
        _candidate(f"slug-{i}", title=f"Title {i}", lines=big_lines) for i in range(10)
    ]
    out = _format_packet(cands, packet_token_ceiling=200)
    # ceiling=200 tokens -> 800 char budget; first entry alone fits but
    # later ones must be truncated.
    assert "1. **Title 0**" in out
    assert "9. **Title 9**" not in out


# ---------------------------------------------------------------------------
# DB-backed integration: build_cue_packet


def _seed_db(db_path: pathlib.Path, notes: list[dict[str, Any]]) -> None:
    """Create a tiny cortex-index.db-shaped DB with the given notes."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE notes (
                rowid INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                folder TEXT NOT NULL,
                title TEXT,
                note_type TEXT,
                status TEXT,
                tags_json TEXT,
                aliases_json TEXT,
                created TEXT,
                updated TEXT,
                body TEXT
            );
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                title, body,
                content='notes',
                content_rowid='rowid',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, title, body)
                VALUES (new.rowid, new.title, new.body);
            END;
            """
        )
        for idx, n in enumerate(notes, start=1):
            conn.execute(
                """
                INSERT INTO notes(rowid, slug, path, folder, title, note_type,
                                  status, tags_json, aliases_json, created,
                                  updated, body)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idx,
                    n["slug"],
                    n.get("path", f"{n['slug']}.md"),
                    n.get("folder", ""),
                    n["title"],
                    n.get("note_type", ""),
                    n.get("status", ""),
                    json.dumps(n.get("tags", [])),
                    json.dumps(n.get("aliases", [])),
                    n.get("created", ""),
                    n.get("updated", ""),
                    n["body"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_vault(vault_root: pathlib.Path, notes: list[dict[str, Any]]) -> None:
    for n in notes:
        rel = n.get("path", f"{n['slug']}.md")
        f = vault_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["---"]
        fm_lines.append(f"title: {n['title']}")
        if n.get("note_type"):
            fm_lines.append(f"note_type: {n['note_type']}")
        if n.get("trigger_keywords"):
            kws = ", ".join(n["trigger_keywords"])
            fm_lines.append(f"trigger_keywords: [{kws}]")
        fm_lines.append("---")
        f.write_text("\n".join(fm_lines) + "\n" + n["body"] + "\n")


@pytest.mark.asyncio
async def test_build_cue_packet_fts_path_returns_packet(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [
        {
            "slug": "cozyhem-arch",
            "title": "CozyHem Architecture",
            "note_type": "research",
            "tags": ["cozyhem"],
            "body": (
                "intro line\n"
                "CozyHem runs on the HA server\n"
                "another line\n"
                "more cozyhem details\n"
            ),
        },
        {
            "slug": "fitness-current-weights",
            "title": "Current Lift Weights",
            "note_type": "daily",
            "tags": ["fitness"],
            "body": "bench press 100\nsquat 200\nrows 80\n",
        },
        {
            "slug": "unrelated-note",
            "title": "Unrelated",
            "note_type": "research",
            "tags": [],
            "body": "this is about something else entirely\n",
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)

    cfg = {
        "enabled": True,
        "top_n": 3,
        "per_note_line_cap": 5,
        "packet_token_ceiling": 1000,
        "timeout_ms": 2000,
    }
    packet = await build_cue_packet(
        "what's in the cozyhem architecture?",
        cfg,
        db_path=db,
        vault_root=vault,
    )
    assert "[VAULT CONTEXT" in packet
    assert "cozyhem-arch" in packet
    assert "CozyHem runs on the HA server" in packet
    # The unrelated note must not appear.
    assert "unrelated-note" not in packet


@pytest.mark.asyncio
async def test_build_cue_packet_disabled_returns_empty(tmp_path: pathlib.Path):
    cfg = {"enabled": False}
    packet = await build_cue_packet("anything", cfg, db_path=tmp_path / "missing.db")
    assert packet == ""


@pytest.mark.asyncio
async def test_build_cue_packet_missing_db_returns_empty(tmp_path: pathlib.Path):
    cfg = {"enabled": True}
    packet = await build_cue_packet(
        "cozyhem", cfg, db_path=tmp_path / "does-not-exist.db"
    )
    assert packet == ""


@pytest.mark.asyncio
async def test_build_cue_packet_dedupes_context_slugs(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [
        {
            "slug": "cozyhem-arch",
            "title": "CozyHem Architecture",
            "body": "cozyhem details here\n",
        },
        {
            "slug": "cozyhem-deploy",
            "title": "CozyHem Deploy",
            "body": "deploy cozyhem like this\n",
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    cfg = {"enabled": True, "top_n": 5}
    packet = await build_cue_packet(
        "cozyhem", cfg, db_path=db, vault_root=vault, context_slugs=["cozyhem-arch"]
    )
    assert "cozyhem-deploy" in packet
    assert "cozyhem-arch" not in packet


@pytest.mark.asyncio
async def test_build_cue_packet_state_note_outranks_research(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """When STATE_BOOST is non-1.0, a daily (state-tier) outranks a plain
    research note. Verifies the boost MECHANISM (not the calibrated
    coefficient — calibrated to 1.0 on 2026-05-06 per
    cortex-memory/research/2026-05-06-cue-runner-eval.md). Test patches
    STATE_BOOST so the mechanism is measurable independent of the default."""
    monkeypatch.setattr("alice_speaking.retrieval.cue_runner.STATE_BOOST", 2.0)
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [
        {
            "slug": "today-daily",
            "title": "Today",
            "note_type": "daily",
            "body": "bench progression keyword keyword keyword\n",
        },
        {
            "slug": "old-research",
            "title": "Old Research",
            "note_type": "research",
            "body": "bench press analysis keyword keyword keyword\n",
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    cfg = {"enabled": True, "top_n": 2}
    packet = await build_cue_packet(
        "bench progression keyword", cfg, db_path=db, vault_root=vault
    )
    # Daily note must appear first (under "1.").
    assert packet.index("today-daily") < packet.index("old-research")


@pytest.mark.asyncio
async def test_build_cue_packet_trigger_keyword_boost_promotes_match(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """When BEHAVIOR_BOOST and TRIGGER_KEYWORD_EXTRA are non-1.0, a behavior
    note with trigger_keywords matching the query outranks a plain bucket1
    note. Verifies the secondary-boost MECHANISM (not the calibrated
    coefficient — calibrated to 1.0 on 2026-05-06 per
    cortex-memory/research/2026-05-06-cue-runner-eval.md). Test patches the
    constants so the mechanism is measurable independent of the default."""
    monkeypatch.setattr("alice_speaking.retrieval.cue_runner.BEHAVIOR_BOOST", 1.5)
    monkeypatch.setattr(
        "alice_speaking.retrieval.cue_runner.TRIGGER_KEYWORD_EXTRA", 1.5
    )
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [
        {
            "slug": "auto-fix-protocol",
            "title": "Auto Fix Protocol",
            "note_type": "behavior",
            "trigger_keywords": ["github", "issue"],
            "body": "auto fix github issue protocol triggers here\n",
        },
        {
            "slug": "github-history",
            "title": "Github History",
            "note_type": "research",
            "body": "github issue tracker history overview\n",
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    cfg = {"enabled": True, "top_n": 2}
    packet = await build_cue_packet(
        "new github issue arrived", cfg, db_path=db, vault_root=vault
    )
    assert packet.index("auto-fix-protocol") < packet.index("github-history")


@pytest.mark.asyncio
async def test_build_cue_packet_no_query_tokens_returns_empty(
    tmp_path: pathlib.Path,
):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "anything"}])
    cfg = {"enabled": True}
    # Query is all stopwords -> no tokens -> empty packet.
    packet = await build_cue_packet("the of to", cfg, db_path=db, vault_root=tmp_path)
    assert packet == ""


# ---------------------------------------------------------------------------
# Phase 2 reranker integration (mocked — no API calls)


@pytest.mark.asyncio
async def test_build_cue_packet_reranker_disabled_by_default(
    tmp_path: pathlib.Path, monkeypatch
):
    """With reranker.enabled=False, _call_reranker must NOT be invoked."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_db(db, [{"slug": "a", "title": "A", "body": "cozyhem here\n"}])
    _seed_vault(vault, [{"slug": "a", "title": "A", "body": "cozyhem here"}])

    called = {"n": 0}

    async def _spy(query, candidates, cfg):
        called["n"] += 1
        return candidates

    monkeypatch.setattr(cue_runner, "_call_reranker", _spy)
    await build_cue_packet("cozyhem", {"enabled": True}, db_path=db, vault_root=vault)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_build_cue_packet_reranker_enabled_invokes_call(
    tmp_path: pathlib.Path, monkeypatch
):
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [
        {"slug": "a", "title": "A", "body": "cozyhem here long enough\n"},
        {"slug": "b", "title": "B", "body": "cozyhem there long enough\n"},
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)

    captured: dict[str, Any] = {}

    async def _spy(query, candidates, cfg):
        captured["query"] = query
        captured["count"] = len(candidates)
        # Reverse order so we can detect that rerank actually applied.
        return list(reversed(candidates))

    monkeypatch.setattr(cue_runner, "_call_reranker", _spy)
    cfg = {
        "enabled": True,
        "top_n": 2,
        "reranker": {"enabled": True, "model": "test-model"},
    }
    packet = await build_cue_packet("cozyhem", cfg, db_path=db, vault_root=vault)
    assert captured["query"] == "cozyhem"
    assert captured["count"] >= 2
    # Reverse means whichever was second now appears first.
    assert packet.index("`b`") < packet.index("`a`") or packet.index(
        "`a`"
    ) < packet.index("`b`")


def test_apply_rerank_drops_hallucinated_slugs():
    cands = [
        _candidate("real-1"),
        _candidate("real-2"),
    ]
    text = json.dumps(
        [
            {"slug": "made-up", "score": 0.9, "why_relevant": "lies"},
            {"slug": "real-2", "score": 0.8, "why_relevant": "good"},
        ]
    )
    out = cue_runner._apply_rerank(text, cands)
    slugs = [c.slug for c in out]
    assert slugs[0] == "real-2"
    assert "made-up" not in slugs
    # The dropped real-1 still gets appended for fallback.
    assert "real-1" in slugs


def test_apply_rerank_malformed_returns_input():
    cands = [_candidate("a")]
    out = cue_runner._apply_rerank("not json at all", cands)
    assert out is cands


# ---------------------------------------------------------------------------
# access_count bump


@pytest.mark.asyncio
async def test_bump_access_increments_count_and_writes_today(
    tmp_path: pathlib.Path,
):
    note = tmp_path / "n.md"
    note.write_text(
        "---\ntitle: N\nlast_accessed: 2020-01-01\naccess_count: 4\n---\nbody\n"
    )
    await _bump_access(tmp_path, "n.md")
    after = note.read_text()
    assert "access_count: 5" in after
    # last_accessed was rewritten to today; the old date is gone.
    assert "2020-01-01" not in after
    assert "body" in after


@pytest.mark.asyncio
async def test_bump_access_appends_fields_when_missing(tmp_path: pathlib.Path):
    note = tmp_path / "n.md"
    note.write_text("---\ntitle: N\n---\nbody\n")
    await _bump_access(tmp_path, "n.md")
    after = note.read_text()
    assert "access_count: 1" in after
    assert "last_accessed:" in after


@pytest.mark.asyncio
async def test_bump_access_no_frontmatter_is_noop(tmp_path: pathlib.Path):
    note = tmp_path / "n.md"
    note.write_text("just a body\n")
    await _bump_access(tmp_path, "n.md")
    assert note.read_text() == "just a body\n"


@pytest.mark.asyncio
async def test_bump_access_missing_file_is_noop(tmp_path: pathlib.Path):
    # Should not raise.
    await _bump_access(tmp_path, "does-not-exist.md")


# ---------------------------------------------------------------------------
# access_count DB write path
#
# The cue runner's bump must update BOTH the markdown frontmatter and
# the note_metrics row keyed by slug. Without the DB write the SQL
# retrieval boost based on access_count cannot fire — see
# cortex-memory/research/2026-05-11-retrieval-data-pipeline-critical.md.


def _make_metrics_db(path: pathlib.Path, slugs: list[str]) -> None:
    """Create a minimal cortex-index-shaped DB with a seeded
    ``note_metrics`` table. Only the columns the bump touches are
    declared — keeps the fixture self-contained without dragging in
    the full indexer schema. The bump uses INSERT ... ON CONFLICT, so
    a pre-existing row exercises the UPDATE branch and a missing slug
    exercises the INSERT branch."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE note_metrics (
                slug TEXT PRIMARY KEY,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_queried TEXT,
                speaking_accessed_at TEXT
            )
            """
        )
        for slug in slugs:
            conn.execute(
                "INSERT INTO note_metrics(slug, access_count) VALUES(?, 0)",
                (slug,),
            )
        conn.commit()
    finally:
        conn.close()


def _read_db_count(db: pathlib.Path, slug: str) -> int | None:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT access_count FROM note_metrics WHERE slug = ?", (slug,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else int(row[0])


@pytest.mark.asyncio
async def test_bump_access_updates_db_when_db_path_given(tmp_path: pathlib.Path):
    """The bump must increment note_metrics.access_count alongside the
    frontmatter when ``db_path`` is supplied."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("---\ntitle: N\naccess_count: 4\n---\nbody\n")
    db = tmp_path / "cortex-index.db"
    _make_metrics_db(db, ["n"])

    await _bump_access(vault, "n.md", db_path=db, slug="n")

    assert "access_count: 5" in note.read_text()
    assert _read_db_count(db, "n") == 1  # was 0, now +1


@pytest.mark.asyncio
async def test_bump_access_db_upserts_missing_row(tmp_path: pathlib.Path):
    """If the DB has no row for the slug (defensive — the indexer
    should always seed one), the bump inserts a fresh row at 1 rather
    than silently swallowing the write."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "fresh.md"
    note.write_text("---\ntitle: Fresh\naccess_count: 0\n---\nbody\n")
    db = tmp_path / "cortex-index.db"
    _make_metrics_db(db, [])  # empty table — no row for "fresh"

    await _bump_access(vault, "fresh.md", db_path=db, slug="fresh")

    assert _read_db_count(db, "fresh") == 1


@pytest.mark.asyncio
async def test_bump_access_skips_db_when_db_path_none(tmp_path: pathlib.Path):
    """The legacy call shape (no db_path) must continue to update only
    the frontmatter — no DB connection attempted."""
    note = tmp_path / "n.md"
    note.write_text("---\ntitle: N\naccess_count: 2\n---\nbody\n")
    # No DB exists; if the bump tried to open one it would error.
    await _bump_access(tmp_path, "n.md")
    assert "access_count: 3" in note.read_text()


@pytest.mark.asyncio
async def test_bump_access_db_write_failure_does_not_block_frontmatter(
    tmp_path: pathlib.Path,
):
    """If the DB path is invalid (e.g. pointing at a non-DB file), the
    frontmatter bump must still succeed. Persistence to either store
    is recoverable from the other."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("---\ntitle: N\naccess_count: 1\n---\nbody\n")
    bogus_db = tmp_path / "not-a-db.txt"
    bogus_db.write_text("definitely not sqlite")

    # Should not raise; should still update frontmatter.
    await _bump_access(vault, "n.md", db_path=bogus_db, slug="n")
    assert "access_count: 2" in note.read_text()
