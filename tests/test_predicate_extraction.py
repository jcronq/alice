"""Tests for :mod:`alice_thinking.memory_worker.predicate_extraction`.

Covers Phase 1 of GBrain-style typed-edge extraction:
  - Schema pack loader (valid + malformed).
  - Verb regex matching for the initial pack (owns, mentions, etc.).
  - Code-block stripping (no edges from ``[[wikilink]]`` inside fences).
  - Idempotency (two runs = same edges).
  - Ignore-list + min_name_length filters honor the "unless the slug
    exists as a real note" override.
  - ReDoS budget aborts remaining patterns without failing extraction.
  - build_index.py produces schema_version=2 with the typed_edges table.

Every test seeds a tmp-path vault + cortex-index.db so no real state
is touched.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from alice_thinking.memory_worker import predicate_extraction as pe
from indexer import build_index


# ---------- fixtures ----------


# The tests read the real schema pack from the repo — it lives in the
# alice-mind vault at ~/alice-mind/config/typed_edges_schema.yaml, which
# is what the memory worker uses in production. Copying it into a
# textwrap.dedent string here would require correctly escaping four
# levels of backslashes (Python source → Python string → YAML → regex),
# which is more error-prone than reading the source of truth.
_REPO_SCHEMA = pathlib.Path.home() / "alice-mind" / "config" / "typed_edges_schema.yaml"
SCHEMA_YAML = _REPO_SCHEMA.read_text(encoding="utf-8") if _REPO_SCHEMA.exists() else ""


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp-path alice-mind with the expected shape."""
    (tmp_path / "cortex-memory" / "projects").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "people").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "reference").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "dailies").mkdir(parents=True)
    (tmp_path / "inner" / "state").mkdir(parents=True)
    (tmp_path / "config").mkdir(parents=True)
    if not SCHEMA_YAML:
        pytest.skip("schema pack not present at ~/alice-mind/config/typed_edges_schema.yaml")
    (tmp_path / "config" / "typed_edges_schema.yaml").write_text(
        SCHEMA_YAML, encoding="utf-8"
    )
    return tmp_path


def _drop_note(mind: pathlib.Path, folder: str, slug: str, body: str) -> pathlib.Path:
    folder_path = mind / "cortex-memory" / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"title: {slug}\n"
        "created: 2026-07-07\n"
        "updated: 2026-07-07\n"
        "---\n\n"
    )
    path = folder_path / f"{slug}.md"
    path.write_text(fm + body, encoding="utf-8")
    return path


def _build_db(mind: pathlib.Path) -> pathlib.Path:
    """Run the indexer against ``mind/cortex-memory`` so typed_edges
    extraction has a live DB to write into."""
    db_path = mind / "inner" / "state" / "cortex-index.db"
    build_index.build(mind / "cortex-memory", db_path)
    return db_path


# ---------- schema pack loader ----------


def test_schema_loader_accepts_valid(mind: pathlib.Path) -> None:
    pack = pe.load_schema_pack(mind / "config" / "typed_edges_schema.yaml")
    assert pack.schema_version == 1
    assert pack.regex_budget_ms == 50
    assert pack.min_name_length == 4
    assert "AI" in pack.ignore_list
    assert "GitHub" in pack.ignore_list
    names = [lt.name for lt in pack.link_types]
    assert names == ["runs", "owns", "contributes_to", "runs_on", "connects_to", "mentions"]


def test_schema_loader_rejects_malformed_regex(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: 1\n"
        "regex_budget_ms: 50\n"
        "min_name_length: 4\n"
        "ignore_list: []\n"
        "link_types:\n"
        "  - name: broken\n"
        '    regex: "((("\n'
        "    target_types: []\n",
        encoding="utf-8",
    )
    with pytest.raises(pe.SchemaPackError) as exc_info:
        pe.load_schema_pack(bad)
    # Error message names the offending verb.
    assert "broken" in str(exc_info.value)


def test_schema_loader_rejects_empty_link_types(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "empty.yaml"
    bad.write_text(
        "schema_version: 1\n"
        "regex_budget_ms: 50\n"
        "min_name_length: 4\n"
        "ignore_list: []\n"
        "link_types: []\n",
        encoding="utf-8",
    )
    with pytest.raises(pe.SchemaPackError):
        pe.load_schema_pack(bad)


# ---------- extraction: verb regexes ----------


def test_owns_edge_extracted(mind: pathlib.Path) -> None:
    _drop_note(mind, "projects", "cozyhem", "cozyhem project")
    _drop_note(mind, "people", "jason", "I run [[cozyhem]] daily.")
    db = _build_db(mind)
    report = pe.extract_all(
        mind / "cortex-memory",
        db,
        mind / "config" / "typed_edges_schema.yaml",
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT from_slug, to_slug, link_type FROM typed_edges "
            "WHERE from_slug='jason' AND to_slug='cozyhem'"
        ).fetchall()
    finally:
        conn.close()
    verbs = {r[2] for r in rows}
    # "I run [[cozyhem]]" matches BOTH runs and owns; the first
    # declaration in the pack (runs) wins per first-match rule.
    assert "runs" in verbs
    assert report.edges_written > 0


def test_wikilink_in_code_block_produces_no_edge(mind: pathlib.Path) -> None:
    _drop_note(mind, "people", "jason", "jason page")
    _drop_note(
        mind,
        "reference",
        "code-sample",
        "Here is a snippet:\n\n```python\nx = '[[jason]]'\n```\n\nend.",
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        mind / "config" / "typed_edges_schema.yaml",
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT from_slug, to_slug FROM typed_edges "
            "WHERE from_slug='code-sample' AND to_slug='jason'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_idempotent_two_runs(mind: pathlib.Path) -> None:
    _drop_note(mind, "projects", "cozyhem", "cozyhem project")
    _drop_note(mind, "people", "jason", "I run [[cozyhem]] daily.")
    db = _build_db(mind)
    schema = mind / "config" / "typed_edges_schema.yaml"
    state = mind / "config" / "typed_edges_state.json"

    pe.extract_all(mind / "cortex-memory", db, schema, state, force_full=True)
    conn = sqlite3.connect(str(db))
    try:
        first = conn.execute("SELECT COUNT(*) FROM typed_edges").fetchone()[0]
    finally:
        conn.close()

    pe.extract_all(mind / "cortex-memory", db, schema, state, force_full=True)
    conn = sqlite3.connect(str(db))
    try:
        second = conn.execute("SELECT COUNT(*) FROM typed_edges").fetchone()[0]
    finally:
        conn.close()
    assert first == second
    assert first > 0


# ---------- ignore-list + min-length filters ----------


def test_ignore_list_ai_skipped_unless_note_exists(mind: pathlib.Path) -> None:
    # No AI.md in the vault, so [[AI]] must be dropped.
    _drop_note(
        mind, "reference", "essay", "Some thoughts about [[AI]] and its future."
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        mind / "config" / "typed_edges_schema.yaml",
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT * FROM typed_edges WHERE to_slug='AI'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_ignore_list_ai_kept_when_note_exists(mind: pathlib.Path) -> None:
    _drop_note(mind, "reference", "AI", "AI concept note.")
    _drop_note(
        mind, "reference", "essay", "Some thoughts about [[AI]] and its future."
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        mind / "config" / "typed_edges_schema.yaml",
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT * FROM typed_edges WHERE to_slug='AI'"
        ).fetchall()
    finally:
        conn.close()
    # Real AI note exists → edge kept.
    assert len(rows) >= 1


def test_github_skipped_unless_note_exists(mind: pathlib.Path) -> None:
    _drop_note(
        mind, "reference", "notes", "Repo hosted on [[GitHub]] for sure."
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        mind / "config" / "typed_edges_schema.yaml",
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT * FROM typed_edges WHERE to_slug='GitHub'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


# ---------- ReDoS guard ----------


def test_redos_budget_bounded(mind: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """A pathological regex should not crash extraction; skipping the
    remaining patterns is expected behavior, not failure."""
    schema_path = mind / "config" / "typed_edges_schema.yaml"
    # Replace pack with one that has a slow pattern early in the list.
    # The 4-backslash sequences in the Python source render as 2
    # backslashes in the YAML text, which the schema loader then
    # decodes to a single backslash before compiling the regex.
    schema_path.write_text(
        "schema_version: 1\n"
        "regex_budget_ms: 1\n"
        "min_name_length: 4\n"
        "ignore_list: []\n"
        "link_types:\n"
        "  - name: slow_first\n"
        '    regex: "(a+)+b\\\\[\\\\[([\\\\w-]+)\\\\]\\\\]"\n'
        "    target_types: []\n"
        "  - name: mentions\n"
        '    regex: "\\\\[\\\\[([\\\\w-]+)\\\\]\\\\]"\n'
        "    target_types: []\n",
        encoding="utf-8",
    )
    _drop_note(mind, "projects", "cozyhem", "cozyhem project")
    # Body includes a wikilink so extraction finds a mention to process.
    body = "aaaaaaaaaaaaaaaaaaaaaaaaaaaa mentions [[cozyhem]] here."
    _drop_note(mind, "people", "jason", body)
    db = _build_db(mind)
    # Should not raise.
    report = pe.extract_all(
        mind / "cortex-memory",
        db,
        schema_path,
        mind / "config" / "typed_edges_state.json",
    )
    # At minimum, extraction returned without crashing. Edges may or
    # may not have been written depending on when the budget tripped;
    # either outcome is acceptable per the design spec.
    assert isinstance(report.edges_written, int)


# ---------- indexer produces v2 schema ----------


def test_build_index_produces_schema_v2_with_typed_edges(mind: pathlib.Path) -> None:
    _drop_note(mind, "reference", "root", "just a note")
    db = _build_db(mind)
    conn = sqlite3.connect(str(db))
    try:
        (version,) = conn.execute(
            "SELECT schema_version FROM meta LIMIT 1"
        ).fetchone()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='typed_edges'"
        ).fetchall()
    finally:
        conn.close()
    assert version == 2
    assert rows == [("typed_edges",)]


def test_typed_edges_unique_constraint(mind: pathlib.Path) -> None:
    """Duplicate INSERT OR IGNORE against the unique-tuple must be a no-op."""
    db = _build_db(mind)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO typed_edges "
            "(from_slug, to_slug, link_type, context) VALUES (?, ?, ?, ?)",
            ("a", "b", "mentions", "b"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO typed_edges "
            "(from_slug, to_slug, link_type, context) VALUES (?, ?, ?, ?)",
            ("a", "b", "mentions", "b"),
        )
        conn.commit()
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM typed_edges "
            "WHERE from_slug='a' AND to_slug='b' AND link_type='mentions'"
        ).fetchone()
    finally:
        conn.close()
    assert count == 1


# ---------- state file ----------


def test_state_file_updated_after_extraction(mind: pathlib.Path) -> None:
    _drop_note(mind, "projects", "cozyhem", "cozyhem project")
    _drop_note(mind, "people", "jason", "I run [[cozyhem]] daily.")
    db = _build_db(mind)
    state_path = mind / "config" / "typed_edges_state.json"
    pe.extract_all(
        mind / "cortex-memory",
        db,
        mind / "config" / "typed_edges_schema.yaml",
        state_path,
    )
    state = json.loads(state_path.read_text())
    assert state["last_run_at"] is not None
    assert state["schema_version"] == 1
    assert state["total_edges_extracted"] >= 1
