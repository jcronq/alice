"""Tests for the index auto-rebuild safety net.

Covers:

1. ``build_index.needs_rebuild`` — the predicate the periodic rebuild
   service (Path B, ``sandbox/s6/alice-index-rebuild/``) gates on via
   ``build_index --check``. A missing DB always rebuilds; a freshly
   built DB does not; adding a note flips it back to rebuild-needed.
2. ``metrics.vault_health._compute_index_staleness`` — the
   observability field emitted on every ``vault_health`` event.

Design: cortex-memory/research/2026-06-21-index-auto-rebuild-implementation-spec.md
"""

from __future__ import annotations

import pathlib

from indexer.build_index import build, needs_rebuild
from metrics.vault_health import _compute_index_staleness


def _write_note(path: pathlib.Path, *, title: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: reference\nstatus: open\ntags: []\n---\n\n{body}\n"
    )


# ---------------------------------------------------------------------------
# needs_rebuild — the --check predicate
# ---------------------------------------------------------------------------


def test_needs_rebuild_true_when_db_missing(tmp_path: pathlib.Path):
    """A non-existent DB always signals rebuild needed."""
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "nonexistent.db"
    assert needs_rebuild(vault, db_path) is True


def test_needs_rebuild_round_trip(tmp_path: pathlib.Path):
    """Fresh build → no rebuild; add a note → rebuild needed again.

    The flip on a new note is caught by the note_count mismatch branch
    of needs_rebuild even when the new file lands in the same wall-clock
    second as the build (so the test is robust to mtime resolution).
    """
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", body="Linked: [[beta]].")
    _write_note(vault / "beta.md", title="Beta")
    db_path = tmp_path / "index.db"

    build(vault, db_path)
    assert needs_rebuild(vault, db_path) is False

    # A newly-written note must flip the predicate back to True.
    _write_note(vault / "gamma.md", title="Gamma")
    assert needs_rebuild(vault, db_path) is True


# ---------------------------------------------------------------------------
# _compute_index_staleness — vault_health observability field
# ---------------------------------------------------------------------------


def test_compute_index_staleness_missing_db_returns_none(tmp_path: pathlib.Path):
    """No DB on disk → None (field is skipped on the event)."""
    assert _compute_index_staleness(tmp_path / "absent.db") is None
    assert _compute_index_staleness(None) is None


def test_compute_index_staleness_fresh_build(tmp_path: pathlib.Path):
    """A fresh build reports low staleness and a matching note count.

    The function derives the vault root as ``db.parent.parent / cortex-memory``,
    so the DB must live at ``<root>/inner/state/`` and the vault at
    ``<root>/cortex-memory/`` to exercise the on-disk count comparison.
    """
    root = tmp_path / "alice-mind"
    vault = root / "cortex-memory"
    db_path = root / "inner" / "state" / "cortex-index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _write_note(vault / "alpha.md", title="Alpha")
    _write_note(vault / "beta.md", title="Beta")
    # A dotfile-prefixed path must be excluded from the expected count,
    # mirroring build_index's own scan rule.
    _write_note(vault / ".consumed" / "old.md", title="Old")

    build(vault, db_path)

    staleness = _compute_index_staleness(db_path)
    assert staleness is not None
    assert staleness["staleness_seconds"] is not None
    assert staleness["staleness_seconds"] >= 0
    assert staleness["staleness_seconds"] < 3600
    assert staleness["note_count_in_index"] == 2
    assert staleness["note_count_expected"] == 2
    # Counts agree → mismatch reported as None (falsy), not True.
    assert staleness["note_count_mismatch"] is None


def test_compute_index_staleness_detects_count_mismatch(tmp_path: pathlib.Path):
    """Adding a note after the build surfaces a note_count_mismatch."""
    root = tmp_path / "alice-mind"
    vault = root / "cortex-memory"
    db_path = root / "inner" / "state" / "cortex-index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _write_note(vault / "alpha.md", title="Alpha")
    build(vault, db_path)

    _write_note(vault / "beta.md", title="Beta")
    staleness = _compute_index_staleness(db_path)
    assert staleness is not None
    assert staleness["note_count_in_index"] == 1
    assert staleness["note_count_expected"] == 2
    assert staleness["note_count_mismatch"] is True
