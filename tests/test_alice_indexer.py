"""Phase 1 of plan 08: alice_indexer smoke tests.

The vault indexer was previously untested; the move from
``alice_core/cortex_index/`` → ``alice_indexer/`` is the right time
to add a small smoke. Three contracts:

1. ``yaml_lite.split_frontmatter`` parses a markdown body with a
   YAML frontmatter block into ``(metadata_dict, body)``.
2. ``build_index.build(vault, db_path)`` produces an SQLite DB
   containing the expected core tables (``notes``, ``links``,
   ``meta``, ``note_metrics``).
3. ``build_index.needs_rebuild`` returns False on a fresh-rebuilt
   DB and True when the DB is missing.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from alice_indexer.build_index import build, needs_rebuild
from alice_indexer.yaml_lite import extract_wikilinks, split_frontmatter


# ---------------------------------------------------------------------------
# yaml_lite


def test_split_frontmatter_extracts_metadata():
    body = (
        "---\n"
        "title: My Note\n"
        "tags: [alpha, beta]\n"
        "---\n"
        "\n"
        "Body content here."
    )
    meta, content = split_frontmatter(body)
    assert meta["title"] == "My Note"
    assert meta["tags"] == ["alpha", "beta"]
    assert content.strip() == "Body content here."


def test_split_frontmatter_no_frontmatter():
    """Plain markdown with no frontmatter returns an empty dict
    and the original body unchanged."""
    body = "# Heading\n\nJust prose, no metadata."
    meta, content = split_frontmatter(body)
    assert meta == {}
    assert content == body


def test_extract_wikilinks_finds_targets():
    body = "See [[foo-note]] and [[bar/baz|baz]] for details."
    links = extract_wikilinks(body)
    assert "foo-note" in links
    # Wikilinks with `|alias` strip the alias and keep the target.
    assert any("bar/baz" in link for link in links)


# ---------------------------------------------------------------------------
# build_index


def _write_note(path: pathlib.Path, *, title: str, body: str = "Hello.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        "type: reference\n"
        "status: open\n"
        "tags: []\n"
        "---\n\n"
        f"{body}\n"
    )


def test_build_creates_expected_schema(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", body="Linked: [[beta]].")
    _write_note(vault / "beta.md", title="Beta")

    db_path = tmp_path / "index.db"
    stats = build(vault, db_path)

    assert db_path.is_file()
    # ``build`` reports stats; the schema is the contract.
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()

    for required in ("notes", "links", "meta", "note_metrics"):
        assert required in tables, (
            f"missing core table {required!r}; stats={stats}, "
            f"tables present: {tables}"
        )


def test_needs_rebuild_false_when_db_fresh(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    build(vault, db_path)
    # Just-built DB → fresh → no rebuild needed.
    assert needs_rebuild(vault, db_path) is False


def test_needs_rebuild_true_when_db_missing(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    # No build() call — DB doesn't exist.
    assert needs_rebuild(vault, db_path) is True


def test_build_raises_when_vault_missing(tmp_path: pathlib.Path):
    """The indexer surfaces a SystemExit (CLI-friendly) when the
    vault path doesn't exist. Same shape the ``--check`` flow
    relies on."""
    db_path = tmp_path / "index.db"
    with pytest.raises(SystemExit, match="vault not found"):
        build(tmp_path / "nonexistent", db_path)
