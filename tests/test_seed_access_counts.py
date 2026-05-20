"""Tests for indexer.seed_access_counts.

The seed is a one-time reconciliation step: copy ``access_count`` from
vault frontmatter into the ``note_metrics`` table that the cue runner
queries. Three contracts:

1. The seed populates ``note_metrics.access_count`` from the
   frontmatter when given a known vault + DB.
2. The seed is idempotent — re-running against an already-seeded DB
   produces the same result (SET semantics, not increment).
3. The seed tolerates notes whose frontmatter omits ``access_count``
   (treated as 0) and rows that are absent from ``note_metrics``
   (upserted on the fly).
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from indexer.build_index import build
from indexer.seed_access_counts import seed


def _write_note(
    path: pathlib.Path, *, title: str, access_count: int | None, body: str = "Hello."
) -> None:
    """Helper: write a vault note with optional access_count frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        f"title: {title}",
        "type: reference",
        "status: open",
        "tags: []",
    ]
    if access_count is not None:
        fm_lines.append(f"access_count: {access_count}")
    fm_lines.append("---")
    path.write_text("\n".join(fm_lines) + f"\n\n{body}\n")


def _read_count(db_path: pathlib.Path, slug: str) -> int | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT access_count FROM note_metrics WHERE slug = ?", (slug,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else int(row[0])


def test_seed_populates_from_frontmatter(tmp_path: pathlib.Path):
    """After PR #196 the indexer itself seeds access_count from
    frontmatter at build time, so the seed step is a reconciliation
    that confirms the same values are present (SET semantics)."""
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", access_count=7)
    _write_note(vault / "beta.md", title="Beta", access_count=3)
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    # Indexer (post-#196) already seeded from frontmatter.
    assert _read_count(db_path, "alpha") == 7
    assert _read_count(db_path, "beta") == 3

    stats = seed(vault, db_path)

    assert _read_count(db_path, "alpha") == 7
    assert _read_count(db_path, "beta") == 3
    assert stats["notes_seen"] == 2
    assert stats["notes_with_nonzero_count"] == 2
    assert stats["max_access_count"] == 7
    assert stats["dry_run"] is False


def test_seed_treats_missing_field_as_zero(tmp_path: pathlib.Path):
    """A note whose frontmatter omits ``access_count`` should land at
    0 in note_metrics — same as the indexer's initial seed value."""
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", access_count=None)
    _write_note(vault / "beta.md", title="Beta", access_count=12)
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    seed(vault, db_path)

    assert _read_count(db_path, "alpha") == 0
    assert _read_count(db_path, "beta") == 12


def test_seed_is_idempotent(tmp_path: pathlib.Path):
    """Running the seed twice in a row must NOT double-count — SET
    semantics, not increment. This is the contract the operator
    relies on when they're not sure whether the seed already ran."""
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", access_count=4)
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    seed(vault, db_path)
    assert _read_count(db_path, "alpha") == 4

    # Second pass: identical state.
    seed(vault, db_path)
    assert _read_count(db_path, "alpha") == 4


def test_seed_dry_run_does_not_write(tmp_path: pathlib.Path):
    """``--dry-run`` returns stats without modifying the DB."""
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", access_count=9)
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    # Indexer (post-#196) already seeded from frontmatter.
    assert _read_count(db_path, "alpha") == 9
    stats = seed(vault, db_path, dry_run=True)

    # DB unchanged by the dry-run seed.
    assert _read_count(db_path, "alpha") == 9
    # Stats still report what would have been written.
    assert stats["dry_run"] is True
    assert stats["max_access_count"] == 9


def test_seed_raises_when_db_missing(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", access_count=1)
    with pytest.raises(SystemExit, match="cortex-index DB not found"):
        seed(vault, tmp_path / "nope.db")


def test_seed_raises_when_vault_missing(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", access_count=1)
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    # Vault removed after build; seed must surface a clear error
    # rather than silently leaving every note at 0.
    with pytest.raises(SystemExit, match="vault not found"):
        seed(tmp_path / "nonexistent", db_path)
