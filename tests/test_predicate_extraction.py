"""Tests for :mod:`alice_thinking.memory_worker.predicate_extraction`.

Covers Phase 1 + Phase 2.0 of GBrain-style typed-edge extraction:
  - Schema pack loader (valid + malformed).
  - Verb regex matching against the shipped pack (``cites``,
    ``contributes_to``, ``connects_to``).
  - Code-block stripping (no edges from ``[[wikilink]]`` inside fences).
  - Idempotency (two runs = same edges).
  - Ignore-list + min_name_length filters honor the "unless the slug
    exists as a real note" override.
  - ReDoS budget aborts remaining patterns without failing extraction.
  - build_index.py produces schema_version=2 with the typed_edges table.

Phase 2.0-specific coverage:
  - The shipped schema drops ``runs`` (was 100% FP on the v2 run).
  - Word-boundary lookbehind blocks substring matches on ``cites`` /
    ``connects_to`` (e.g., "reconnects to" no longer matches).
  - ``target_types`` enforcement in :func:`extract_from_body` — positive,
    negative, and empty-list (NOOP) cases.

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
    # v3 = Phase 2.0 (dropped ``runs``, added word-boundary lookbehind).
    assert pack.schema_version == 3
    assert pack.regex_budget_ms == 50
    assert pack.min_name_length == 4
    assert "AI" in pack.ignore_list
    assert "GitHub" in pack.ignore_list
    names = [lt.name for lt in pack.link_types]
    assert "runs" not in names, "runs was dropped in Phase 2.0 (100% FP on v2 run)"
    # Order matters (first-match wins); assert exact declaration order.
    assert names == ["cites", "contributes_to", "connects_to"]


def test_schema_loader_rejects_malformed_regex(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: 3\n"
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
        "schema_version: 3\n"
        "regex_budget_ms: 50\n"
        "min_name_length: 4\n"
        "ignore_list: []\n"
        "link_types: []\n",
        encoding="utf-8",
    )
    with pytest.raises(pe.SchemaPackError):
        pe.load_schema_pack(bad)


def test_schema_drops_runs_verb(mind: pathlib.Path) -> None:
    """Phase 2.0: ``runs`` was dropped because all 5 v2 edges validated FP.

    Concrete guard so a future edit that adds it back has to justify
    the change against a test failure — not just a code review skim.
    """
    pack = pe.load_schema_pack(mind / "config" / "typed_edges_schema.yaml")
    assert not any(lt.name == "runs" for lt in pack.link_types)


# ---------- extraction: verb regexes ----------


def test_cites_edge_extracted(mind: pathlib.Path) -> None:
    """``cites`` is the highest-precision verb in the v3 pack (12/12 in
    the v2 audit). A note body with an explicit provenance phrase
    ("validated by") targeting an existing vault slug should produce
    exactly one ``cites`` edge.
    """
    _drop_note(mind, "reference", "graduated-response", "source of truth")
    _drop_note(
        mind,
        "reference",
        "degradation-api",
        "The migration path was validated by [[graduated-response]] before merge.",
    )
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
            "WHERE from_slug='degradation-api' AND to_slug='graduated-response'"
        ).fetchall()
    finally:
        conn.close()
    verbs = {r[2] for r in rows}
    assert "cites" in verbs
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
    _drop_note(mind, "reference", "src-note", "provenance source")
    _drop_note(
        mind,
        "reference",
        "claim-note",
        "Argument sourced from [[src-note]] for the record.",
    )
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
    # No AI.md in the vault, so [[AI]] must be dropped even though the
    # essay body triggers the ``cites`` verb.
    _drop_note(
        mind, "reference", "essay", "Argument sourced from [[AI]] research."
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
        mind, "reference", "essay", "Argument sourced from [[AI]] research."
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
    # Real AI note exists → ignore_list override honored → edge kept.
    assert len(rows) >= 1


def test_github_skipped_unless_note_exists(mind: pathlib.Path) -> None:
    _drop_note(
        mind, "reference", "notes", "Snippet sourced from [[GitHub]] for sure."
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
        "schema_version: 3\n"
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
    _drop_note(mind, "reference", "src-note", "provenance source")
    _drop_note(
        mind,
        "reference",
        "claim-note",
        "Argument sourced from [[src-note]] for the record.",
    )
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
    # v3 = Phase 2.0 (dropped ``runs``, added word-boundary lookbehind).
    assert state["schema_version"] == 3
    assert state["total_edges_extracted"] >= 1


# ---------- Phase 2.0: word-boundary lookbehind ----------


def test_word_boundary_blocks_substring_verb_match(mind: pathlib.Path) -> None:
    """The v3 ``connects_to`` regex is anchored by ``(?:^|(?<= ))`` so a
    verb-prefix inside a longer word — the "dry-run" class of false
    positive that killed the ``runs`` verb — cannot fire an edge.

    Concrete: ``"reconnects to [[bar]]"`` reads to a human as a
    single-word action ("reconnects", verb=reconnect). Without the
    lookbehind the regex would still latch onto "connects to" starting
    at position 2, misattributing the edge as a ``connects_to``. With
    the lookbehind the substring is preceded by "re" (not space, not
    BOL) → no match.
    """
    _drop_note(mind, "reference", "bar", "target for the reconnects clause")
    _drop_note(
        mind,
        "reference",
        "essay",
        "The service reconnects to [[bar]] on every reload.",
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
            "SELECT link_type FROM typed_edges "
            "WHERE from_slug='essay' AND to_slug='bar'"
        ).fetchall()
    finally:
        conn.close()
    verbs = {r[0] for r in rows}
    assert "connects_to" not in verbs, (
        f"word-boundary lookbehind should block substring match on "
        f"'reconnects to'; got verbs={verbs!r}"
    )


# ---------- Phase 2.0: target_types enforcement ----------


def _write_target_types_schema(
    path: pathlib.Path, verb_name: str, target_types: list[str]
) -> None:
    """Write a minimal schema pack with a single verb + declared
    target_types. The verb regex is ``example [[X]]`` so tests can craft
    a body that reliably triggers it without accidentally matching one
    of the shipped verbs."""
    types_yaml = "[" + ", ".join(target_types) + "]"
    path.write_text(
        "schema_version: 3\n"
        "regex_budget_ms: 50\n"
        "min_name_length: 4\n"
        "ignore_list: []\n"
        "link_types:\n"
        f"  - name: {verb_name}\n"
        '    regex: "(?:^|(?<= ))example ?`?\\\\[\\\\[([\\\\w-]+)\\\\]\\\\]`?"\n'
        f"    target_types: {types_yaml}\n",
        encoding="utf-8",
    )


def test_target_types_positive_target_in_declared_folder(
    mind: pathlib.Path,
) -> None:
    """target_types=[projects] + target in projects/ → edge kept."""
    schema = mind / "config" / "typed_edges_schema.yaml"
    _write_target_types_schema(schema, "example", ["projects"])
    _drop_note(mind, "projects", "cozyhem", "the cozyhem project")
    _drop_note(
        mind,
        "reference",
        "src",
        "This is an example [[cozyhem]] mention.",
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        schema,
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT link_type FROM typed_edges "
            "WHERE from_slug='src' AND to_slug='cozyhem'"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["example"]


def test_target_types_negative_target_in_other_folder(
    mind: pathlib.Path,
) -> None:
    """target_types=[projects] + target in research/ → edge dropped."""
    schema = mind / "config" / "typed_edges_schema.yaml"
    _write_target_types_schema(schema, "example", ["projects"])
    (mind / "cortex-memory" / "research").mkdir(parents=True, exist_ok=True)
    _drop_note(mind, "research", "some-research", "a research note")
    _drop_note(
        mind,
        "reference",
        "src",
        "This is an example [[some-research]] mention.",
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        schema,
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT link_type FROM typed_edges "
            "WHERE from_slug='src' AND to_slug='some-research'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [], (
        f"target_types=[projects] should block a target in research/; "
        f"got rows={rows!r}"
    )


def test_target_types_empty_list_accepts_any_target(
    mind: pathlib.Path,
) -> None:
    """target_types=[] preserves pre-Phase-2.0 behavior — no filtering,
    any folder accepted. Guards against a regression that ships an
    unintentionally restrictive default."""
    schema = mind / "config" / "typed_edges_schema.yaml"
    _write_target_types_schema(schema, "example", [])
    (mind / "cortex-memory" / "research").mkdir(parents=True, exist_ok=True)
    _drop_note(mind, "research", "any-note", "a note anywhere")
    _drop_note(
        mind,
        "reference",
        "src",
        "This is an example [[any-note]] mention.",
    )
    db = _build_db(mind)
    pe.extract_all(
        mind / "cortex-memory",
        db,
        schema,
        mind / "config" / "typed_edges_state.json",
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT link_type FROM typed_edges "
            "WHERE from_slug='src' AND to_slug='any-note'"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["example"]
