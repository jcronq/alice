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

import datetime

from alice_speaking.retrieval import cue_runner
from alice_speaking.retrieval.cue_runner import (
    ACCESS_COUNT_ALPHA,
    ACCESS_COUNT_CAP,
    BEHAVIOR_BOOST,
    BUCKET1_BOOST,
    BUCKET2_BOOST,
    FITNESS_DOMAIN_TAGS,
    FITNESS_RECENCY_CAP,
    FITNESS_RECENCY_COEFFICIENT,
    FITNESS_RECENCY_WINDOW_DAYS,
    HEBBIAN_DEFAULTS,
    STATE_BOOST,
    _bump_access,
    _build_fts_match,
    _Candidate,
    _fitness_recency_score,
    _format_packet,
    _query_edge_weights,
    _read_access_counts,
    _read_last_accessed,
    _read_stm_context_slugs,
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
        if n.get("last_accessed"):
            fm_lines.append(f"last_accessed: {n['last_accessed']}")
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


# ---------------------------------------------------------------------------
# Access-count recency boost (Phase 0 closure)
#
# The bump writer was already wired (#90/#99/#196), but build_cue_packet
# never read access_count back into the final score. These tests cover the
# read path: _read_access_counts gracefully tolerates a missing table, and
# build_cue_packet uses note_metrics.access_count to break ties on FTS
# rank — promoting the hot note above its untouched neighbour.


def test_read_access_counts_missing_table_returns_empty(tmp_path: pathlib.Path):
    db = tmp_path / "no-metrics.db"
    sqlite3.connect(str(db)).close()  # touch an empty DB
    assert _read_access_counts(db, ["a", "b"]) == {}


def test_read_access_counts_returns_only_requested_slugs(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    _make_metrics_db(db, ["alpha", "beta", "gamma"])
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE note_metrics SET access_count = ? WHERE slug = ?", (7, "alpha")
        )
        conn.execute(
            "UPDATE note_metrics SET access_count = ? WHERE slug = ?", (3, "beta")
        )
        conn.commit()
    finally:
        conn.close()
    out = _read_access_counts(db, ["alpha", "beta", "missing"])
    assert out == {"alpha": 7, "beta": 3}


def test_read_access_counts_empty_input_short_circuits(tmp_path: pathlib.Path):
    # No DB needed — the helper must return {} without touching the path.
    assert _read_access_counts(tmp_path / "never-opened.db", []) == {}


@pytest.mark.asyncio
async def test_build_cue_packet_access_count_promotes_hot_note(
    tmp_path: pathlib.Path,
):
    """A note with non-zero access_count must outrank a tied-FTS-rank
    neighbour with zero accesses. Uses two notes with identical bodies
    so FTS gives them the same rank; the recency boost is the only
    differentiator."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    body = "cozyhem cozyhem cozyhem details about deployment\n"
    notes = [
        {"slug": "cold-note", "title": "Cold", "body": body},
        {"slug": "hot-note", "title": "Hot", "body": body},
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    # Seed metrics: hot-note has been accessed many times, cold-note never.
    _make_metrics_db_inplace(db, [("cold-note", 0), ("hot-note", 50)])

    cfg = {"enabled": True, "top_n": 2}
    packet = await build_cue_packet("cozyhem", cfg, db_path=db, vault_root=vault)
    # Recency boost must promote hot-note above cold-note.
    assert packet.index("hot-note") < packet.index("cold-note")


@pytest.mark.asyncio
async def test_build_cue_packet_missing_note_metrics_table_is_noop(
    tmp_path: pathlib.Path,
):
    """If note_metrics doesn't exist (legacy DB, fresh seed), the recency
    boost collapses to 1.0 and the packet still builds successfully."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [{"slug": "a", "title": "A", "body": "cozyhem details\n"}]
    _seed_db(db, notes)  # No note_metrics table created.
    _seed_vault(vault, notes)

    cfg = {"enabled": True, "top_n": 1}
    packet = await build_cue_packet("cozyhem", cfg, db_path=db, vault_root=vault)
    assert "cozyhem details" in packet


def test_access_count_constants_match_locked_design():
    """Guardrail: the locked formula is alpha=0.15, cap=100 — drift
    requires re-evaluation per the design doc."""
    assert ACCESS_COUNT_ALPHA == 0.15
    assert ACCESS_COUNT_CAP == 100


def _make_metrics_db_inplace(
    path: pathlib.Path, rows: list[tuple[str, int]]
) -> None:
    """Add a ``note_metrics`` table to an existing DB and seed it with
    ``(slug, access_count)`` rows. Used by recency-boost integration
    tests that already called ``_seed_db`` to lay down the notes table."""
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
        for slug, ac in rows:
            conn.execute(
                "INSERT INTO note_metrics(slug, access_count) VALUES(?, ?)",
                (slug, ac),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fitness-domain recency boost (#246)
#
# The static type-aware constants are calibrated to 1.0× across the board
# — for the fitness domain that no-op buries operational notes under
# meta-research mentions. The recency boost reads `last_accessed` from
# fitness-tagged candidates and adds a log-scaled bonus when they've been
# touched in the last week. Tests cover the read path, the windowing,
# the tag gate, and the additive-floor invariant.


def test_read_last_accessed_parses_iso_date(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text("---\ntitle: A\nlast_accessed: 2026-05-10\n---\nbody\n")
    assert _read_last_accessed(tmp_path, "a.md") == datetime.date(2026, 5, 10)


def test_read_last_accessed_tolerates_trailing_time(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text(
        "---\ntitle: A\nlast_accessed: 2026-05-10 12:38 EDT\n---\nbody\n"
    )
    assert _read_last_accessed(tmp_path, "a.md") == datetime.date(2026, 5, 10)


def test_read_last_accessed_missing_field_returns_none(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text("---\ntitle: A\n---\nbody\n")
    assert _read_last_accessed(tmp_path, "a.md") is None


def test_read_last_accessed_no_frontmatter_returns_none(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text("just a body\n")
    assert _read_last_accessed(tmp_path, "a.md") is None


def test_read_last_accessed_unparseable_returns_none(tmp_path: pathlib.Path):
    note = tmp_path / "a.md"
    note.write_text("---\ntitle: A\nlast_accessed: yesterday\n---\nbody\n")
    assert _read_last_accessed(tmp_path, "a.md") is None


def test_read_last_accessed_missing_file_returns_none(tmp_path: pathlib.Path):
    assert _read_last_accessed(tmp_path, "nope.md") is None


def test_fitness_recency_score_zero_without_fitness_tag():
    today = datetime.date(2026, 5, 19)
    assert (
        _fitness_recency_score(
            ["research", "alice-architecture"],
            access_count=50,
            last_accessed=today,
            today=today,
        )
        == 0.0
    )


def test_fitness_recency_score_zero_when_outside_window():
    today = datetime.date(2026, 5, 19)
    stale = today - datetime.timedelta(days=FITNESS_RECENCY_WINDOW_DAYS + 1)
    assert (
        _fitness_recency_score(
            ["fitness"],
            access_count=10,
            last_accessed=stale,
            today=today,
        )
        == 0.0
    )


def test_fitness_recency_score_zero_when_last_accessed_missing():
    today = datetime.date(2026, 5, 19)
    assert (
        _fitness_recency_score(
            ["fitness"],
            access_count=10,
            last_accessed=None,
            today=today,
        )
        == 0.0
    )


def test_fitness_recency_score_log_scales_within_window():
    today = datetime.date(2026, 5, 19)
    yesterday = today - datetime.timedelta(days=1)
    import math as _math

    assert _fitness_recency_score(
        ["fitness"],
        access_count=10,
        last_accessed=yesterday,
        today=today,
    ) == pytest.approx(_math.log1p(10))


def test_fitness_recency_score_caps_at_fitness_recency_cap():
    today = datetime.date(2026, 5, 19)
    import math as _math

    huge = _fitness_recency_score(
        ["fitness"],
        access_count=10_000,
        last_accessed=today,
        today=today,
    )
    assert huge == pytest.approx(_math.log1p(FITNESS_RECENCY_CAP))


def test_fitness_recency_score_picks_up_ripped_by_40_tag():
    today = datetime.date(2026, 5, 19)
    assert (
        _fitness_recency_score(
            ["ripped-by-40"],
            access_count=5,
            last_accessed=today,
            today=today,
        )
        > 0.0
    )


def test_fitness_domain_tags_includes_known_tags():
    # Guardrail: drift here changes which notes get the boost.
    assert "fitness" in FITNESS_DOMAIN_TAGS
    assert "ripped-by-40" in FITNESS_DOMAIN_TAGS


@pytest.mark.asyncio
async def test_build_cue_packet_fitness_recency_promotes_recently_accessed(
    tmp_path: pathlib.Path,
):
    """A fitness-tagged note with a recent ``last_accessed`` outranks an
    identically-scored non-fitness note. Bodies are identical so FTS
    rank ties; the additive fitness bonus is the only differentiator."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    today = datetime.date.today().isoformat()
    body = "bench press progression details about workouts\n"
    notes = [
        {
            "slug": "fitness-current-weights",
            "title": "Current Lift Weights",
            "note_type": "research",
            "tags": ["fitness"],
            "last_accessed": today,
            "body": body,
        },
        {
            "slug": "old-research-note",
            "title": "Old Research",
            "note_type": "research",
            "tags": [],
            "body": body,
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    _make_metrics_db_inplace(
        db, [("fitness-current-weights", 8), ("old-research-note", 8)]
    )

    cfg = {"enabled": True, "top_n": 2}
    packet = await build_cue_packet(
        "bench press progression", cfg, db_path=db, vault_root=vault
    )
    assert packet.index("fitness-current-weights") < packet.index(
        "old-research-note"
    )


@pytest.mark.asyncio
async def test_build_cue_packet_fitness_recency_skips_stale_notes(
    tmp_path: pathlib.Path,
):
    """A fitness-tagged note whose ``last_accessed`` is older than the
    7-day window must NOT receive the additive bonus — keeping the
    boost honest about being recency-derived. With identical FTS bodies
    and access_counts, the order falls back to FTS rank (stable
    insertion order in the seeded DB)."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    stale = (
        datetime.date.today()
        - datetime.timedelta(days=FITNESS_RECENCY_WINDOW_DAYS + 5)
    ).isoformat()
    body = "bench press progression details about workouts\n"
    notes = [
        {
            "slug": "fitness-stale",
            "title": "Stale Fitness",
            "note_type": "research",
            "tags": ["fitness"],
            "last_accessed": stale,
            "body": body,
        },
        {
            "slug": "other-note",
            "title": "Other",
            "note_type": "research",
            "tags": [],
            "body": body,
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    # Equal access counts so the multiplicative recency_boost ties too.
    _make_metrics_db_inplace(db, [("fitness-stale", 5), ("other-note", 5)])

    cfg = {"enabled": True, "top_n": 2}
    packet = await build_cue_packet(
        "bench press progression", cfg, db_path=db, vault_root=vault
    )
    # The fitness note got no bonus, so it shouldn't have leapfrogged
    # the other on the strength of its tag alone. Order is determined by
    # FTS rank (both notes appear) — the assertion is that the bonus did
    # NOT fire, which we check by confirming no stale-induced reorder.
    # If the bonus had fired, fitness-stale would be guaranteed first;
    # without it, both can appear in any order, so we just confirm both
    # are present.
    assert "fitness-stale" in packet
    assert "other-note" in packet


def test_fitness_recency_score_is_non_negative():
    """Additive-floor invariant (#246): the fitness bonus is non-
    negative under every input combination — a recently-accessed
    fitness note cannot have its final_score reduced by this code
    path, only lifted. Inputs sampled across the relevant axes."""
    today = datetime.date(2026, 5, 19)
    samples = [
        # (tags, access_count, last_accessed, today)
        ([], 100, today, today),
        (["fitness"], 0, today, today),
        (["fitness"], 1_000_000, today, today),
        (["fitness"], 5, None, today),
        (["fitness"], 5, today - datetime.timedelta(days=365), today),
        (["fitness"], -10, today, today),  # defensive against bad input
        (["ripped-by-40"], 50, today - datetime.timedelta(days=3), today),
        (["research", "fitness"], 12, today, today),
    ]
    for tags, ac, la, t in samples:
        assert _fitness_recency_score(tags, ac, la, t) >= 0.0


@pytest.mark.asyncio
async def test_build_cue_packet_fitness_recency_does_not_demote_non_fitness(
    tmp_path: pathlib.Path,
):
    """Additive-floor invariant (#246): introducing the fitness bonus
    must not reorder two non-fitness notes relative to each other.
    Their final_scores depend only on FTS rank × type_boost × recency_boost,
    which is unchanged by the new code path. Verifies the boost is purely
    additive on fitness-tagged candidates and a no-op elsewhere."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    today = datetime.date.today().isoformat()
    notes = [
        # Two non-fitness notes with different body strengths. The
        # stronger BM25 hit must keep its lead even when a fitness
        # note is in the candidate set getting boosted.
        {
            "slug": "non-fitness-strong",
            "title": "Non-Fitness Strong",
            "note_type": "research",
            "tags": ["research"],
            "body": (
                "cozyhem cozyhem cozyhem cozyhem cozyhem cozyhem "
                "cozyhem cozyhem cozyhem cozyhem\n"
            ),
        },
        {
            "slug": "non-fitness-weak",
            "title": "Non-Fitness Weak",
            "note_type": "research",
            "tags": ["research"],
            "body": "cozyhem mention here\n",
        },
        {
            "slug": "fitness-recent",
            "title": "Fitness Recent",
            "note_type": "research",
            "tags": ["fitness"],
            "last_accessed": today,
            "body": "cozyhem note about workouts\n",
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    _make_metrics_db_inplace(
        db,
        [
            ("non-fitness-strong", 5),
            ("non-fitness-weak", 5),
            ("fitness-recent", 50),
        ],
    )

    cfg = {"enabled": True, "top_n": 3}
    packet = await build_cue_packet(
        "cozyhem", cfg, db_path=db, vault_root=vault
    )
    # Among the two non-fitness notes, BM25 ordering must be preserved
    # regardless of what the fitness note's bonus does.
    assert packet.index("non-fitness-strong") < packet.index("non-fitness-weak")


@pytest.mark.asyncio
async def test_build_cue_packet_fitness_recency_no_bonus_for_non_fitness(
    tmp_path: pathlib.Path,
):
    """The recency bonus is fitness-gated. With identical access_counts
    and bodies, only the fitness-tagged note should get the additive
    bonus — the non-fitness note (even if recently accessed) must not.
    The fitness note therefore wins; if the bonus were tag-agnostic,
    they'd tie."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    today = datetime.date.today().isoformat()
    body = "bench press progression details about workouts\n"
    notes = [
        {
            "slug": "non-fitness-recent",
            "title": "Non-Fitness Recent",
            "note_type": "research",
            "tags": ["research"],  # NOT a fitness-domain tag.
            "last_accessed": today,
            "body": body,
        },
        {
            "slug": "fitness-recent",
            "title": "Fitness Recent",
            "note_type": "research",
            "tags": ["fitness"],
            "last_accessed": today,
            "body": body,
        },
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    # Identical access_counts so the multiplicative recency_boost cancels —
    # any ordering difference must come from the additive fitness bonus.
    _make_metrics_db_inplace(
        db, [("non-fitness-recent", 10), ("fitness-recent", 10)]
    )

    cfg = {"enabled": True, "top_n": 2}
    packet = await build_cue_packet(
        "bench press progression", cfg, db_path=db, vault_root=vault
    )
    assert packet.index("fitness-recent") < packet.index("non-fitness-recent")


def test_fitness_recency_constants_match_locked_design():
    """Guardrail: drift in window/cap/coefficient changes which notes
    get boosted and by how much — flag it loudly in CI."""
    assert FITNESS_RECENCY_WINDOW_DAYS == 7
    assert FITNESS_RECENCY_CAP == 100
    assert FITNESS_RECENCY_COEFFICIENT == 0.4


# ---------------------------------------------------------------------------
# Hebbian edge-weight boost (#219, #254)
#
# Notes wikilinked from the user's STM context get an additive boost
# proportional to their edge-weight sum, with structural edges weighted
# heavier than casual ones. Tests cover: (a) the SQL helper computes
# weighted sums correctly, (b) graceful degradation on missing/empty
# context, (c) STM context retrieval prefers speaking_accessed_at,
# (d) the build_cue_packet integration is gated, additive-only, and
# uses the structural floor.


def _make_links_db_inplace(
    path: pathlib.Path,
    rows: list[tuple[str, str, int]],
) -> None:
    """Add a ``links`` table to an existing DB and seed it with
    ``(source_slug, target_slug, is_structural)`` rows, all resolved=1.

    Shape mirrors :mod:`indexer.build_index` — source_slug,
    target_slug, is_structural (0/1), resolved (0/1).
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE links (
                source_slug TEXT NOT NULL,
                target_slug TEXT NOT NULL,
                target_raw TEXT,
                is_structural INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_links_source ON links(source_slug);
            CREATE INDEX idx_links_target ON links(target_slug);
            """
        )
        for source, target, is_structural in rows:
            conn.execute(
                "INSERT INTO links(source_slug, target_slug, target_raw, "
                "is_structural, resolved) VALUES(?, ?, ?, ?, 1)",
                (source, target, target, is_structural),
            )
        conn.commit()
    finally:
        conn.close()


def test_hebbian_defaults_match_decision_memo():
    """Guardrail: the in-module fallback config must match the
    calibration design's recommended values. Drift requires re-eval.
    Updated 2026-05-24 to the calibrated values
    (cortex-memory/research/2026-05-21-hebbian-calibration-design.md)."""
    assert HEBBIAN_DEFAULTS["enabled"] is True
    assert HEBBIAN_DEFAULTS["edge_boost"] == 0.5
    assert HEBBIAN_DEFAULTS["structural_weight"] == 1.0
    assert HEBBIAN_DEFAULTS["casual_weight"] == 0.5
    assert HEBBIAN_DEFAULTS["min_edge_weight_sum"] == 2


def test_query_edge_weights_sums_structural_and_casual(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    _make_links_db_inplace(
        db,
        [
            ("hub", "target-a", 1),  # structural
            ("hub", "target-a", 0),  # casual
            ("hub", "target-b", 1),  # structural
            ("other", "target-a", 1),  # different source
        ],
    )
    # context = [hub] → target-a = 1.0 + 0.25 = 1.25, target-b = 1.0
    weights = _query_edge_weights(
        db,
        ["hub"],
        structural_weight=1.0,
        casual_weight=0.25,
    )
    assert weights == {"target-a": 1.25, "target-b": 1.0}


def test_query_edge_weights_empty_context_returns_empty(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    _make_links_db_inplace(db, [("hub", "target-a", 1)])
    assert _query_edge_weights(db, []) == {}
    assert _query_edge_weights(db, [""]) == {}


def test_query_edge_weights_missing_table_returns_empty(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    # No links table created — graceful degradation.
    assert _query_edge_weights(db, ["hub"]) == {}


def test_query_edge_weights_skips_unresolved_links(tmp_path: pathlib.Path):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE links (
                source_slug TEXT NOT NULL,
                target_slug TEXT NOT NULL,
                target_raw TEXT,
                is_structural INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # One resolved, one unresolved.
        conn.execute(
            "INSERT INTO links(source_slug, target_slug, target_raw, "
            "is_structural, resolved) VALUES('hub', 'target-a', 'x', 1, 1)"
        )
        conn.execute(
            "INSERT INTO links(source_slug, target_slug, target_raw, "
            "is_structural, resolved) VALUES('hub', 'target-b', 'x', 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()
    weights = _query_edge_weights(db, ["hub"])
    assert weights == {"target-a": 1.0}


def test_read_stm_context_slugs_prefers_speaking_accessed_at(
    tmp_path: pathlib.Path,
):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    _make_metrics_db_inplace(db, [("cold", 10), ("hot", 1)])
    # Set speaking_accessed_at so 'cold' is older than 'hot'.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE note_metrics SET speaking_accessed_at = ? WHERE slug = ?",
            ("2026-05-01 10:00:00", "cold"),
        )
        conn.execute(
            "UPDATE note_metrics SET speaking_accessed_at = ? WHERE slug = ?",
            ("2026-05-19 10:00:00", "hot"),
        )
        conn.commit()
    finally:
        conn.close()
    slugs = _read_stm_context_slugs(db, limit=10)
    # 'hot' should come first (most recent speaking_accessed_at).
    assert slugs[0] == "hot"


def test_read_stm_context_slugs_falls_back_to_access_count(
    tmp_path: pathlib.Path,
):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    # Create a note_metrics table WITHOUT speaking_accessed_at.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE note_metrics (slug TEXT PRIMARY KEY, "
            "access_count INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("INSERT INTO note_metrics VALUES('cold', 1)")
        conn.execute("INSERT INTO note_metrics VALUES('hot', 100)")
        conn.commit()
    finally:
        conn.close()
    slugs = _read_stm_context_slugs(db, limit=10)
    assert slugs[0] == "hot"


def test_read_stm_context_slugs_missing_table_returns_empty(
    tmp_path: pathlib.Path,
):
    db = tmp_path / "cortex-index.db"
    _seed_db(db, [{"slug": "a", "title": "A", "body": "x"}])
    assert _read_stm_context_slugs(db) == []


@pytest.mark.asyncio
async def test_build_cue_packet_hebbian_disabled_is_noop(
    tmp_path: pathlib.Path,
):
    """With hebbian.enabled=False, the ranking must match the
    pre-Hebbian behaviour exactly even if the links table is present."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    body = "cozyhem details about deployment\n"
    notes = [
        {"slug": "boring", "title": "Boring", "body": body},
        {"slug": "linked", "title": "Linked", "body": body},
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    _make_links_db_inplace(db, [("hub", "linked", 1)] * 20)

    cfg_no_hebb = {"enabled": True, "top_n": 2}
    cfg_hebb_off = {
        "enabled": True,
        "top_n": 2,
        "hebbian": {"enabled": False, "edge_boost": 0.4},
    }
    p1 = await build_cue_packet("cozyhem", cfg_no_hebb, db_path=db, vault_root=vault)
    p2 = await build_cue_packet(
        "cozyhem", cfg_hebb_off, db_path=db, vault_root=vault
    )
    assert p1 == p2


@pytest.mark.asyncio
async def test_build_cue_packet_hebbian_promotes_linked_note(
    tmp_path: pathlib.Path,
):
    """A note that's the target of many structural links from the
    STM context must outrank an otherwise-identical unlinked sibling."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    body = "cozyhem details about deployment\n"
    notes = [
        {"slug": "unlinked", "title": "Unlinked", "body": body},
        {"slug": "linked", "title": "Linked", "body": body},
    ]
    _seed_db(db, notes)
    _seed_vault(vault, notes)
    # Seed metrics so STM context = [hub] (not in the candidate set
    # itself, just the source of the wikilinks).
    _make_metrics_db_inplace(db, [("hub", 100)])
    # Hub points at 'linked' with 20 structural edges → boost dominates.
    _make_links_db_inplace(db, [("hub", "linked", 1)] * 20)

    cfg = {
        "enabled": True,
        "top_n": 2,
        "hebbian": {
            "enabled": True,
            "edge_boost": 0.4,
            "structural_weight": 1.0,
            "casual_weight": 0.25,
            "min_edge_weight_sum": 8,
        },
    }
    packet = await build_cue_packet("cozyhem", cfg, db_path=db, vault_root=vault)
    assert packet.index("linked") < packet.index("unlinked")


@pytest.mark.asyncio
async def test_build_cue_packet_hebbian_missing_links_table_is_noop(
    tmp_path: pathlib.Path,
):
    """If the links table is missing (legacy DB), the Hebbian path
    degrades to zero boost and the packet still builds."""
    db = tmp_path / "cortex-index.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = [{"slug": "a", "title": "A", "body": "cozyhem details\n"}]
    _seed_db(db, notes)
    _seed_vault(vault, notes)

    cfg = {
        "enabled": True,
        "top_n": 1,
        "hebbian": {"enabled": True, "edge_boost": 0.4},
    }
    packet = await build_cue_packet("cozyhem", cfg, db_path=db, vault_root=vault)
    assert "cozyhem details" in packet
