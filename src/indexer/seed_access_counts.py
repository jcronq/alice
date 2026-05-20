#!/usr/bin/env python3
"""One-time seed for note_metrics.access_count from existing frontmatter.

Walks every note in the vault (~/alice-mind/cortex-memory/), parses the
``access_count: N`` value out of each frontmatter, and writes N into the
``note_metrics.access_count`` row keyed by slug.

Idempotency contract: SET, not INCREMENT. Re-running the seed against an
already-seeded vault is a no-op — each note's row ends up at exactly the
frontmatter value, regardless of how many times the script has run.

When to run: once, at deploy of PR #90 (the cue-runner SQLite-write
change). Not part of regular maintenance — after the seed, the cue
runner keeps the column live on its own.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Reuse the indexer's default paths + frontmatter parser so the seed
# tooling tracks the indexer's contract exactly.
from indexer.build_index import DEFAULT_DB, DEFAULT_VAULT
from indexer.yaml_lite import split_frontmatter


def _coerce_access_count(raw: object) -> int:
    """Best-effort coercion of a frontmatter ``access_count`` value to
    a non-negative int.

    ``yaml_lite._parse_scalar`` already returns an int for bare integer
    fields, but defensive coercion keeps this script robust against
    notes that quoted the value or wrote a non-numeric string.
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return 0
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return 0
        try:
            return max(0, int(s))
        except ValueError:
            return 0
    return 0


def _read_access_count(note_path: Path) -> int:
    """Return the ``access_count`` value from a note's frontmatter, or
    0 if the file is missing, unreadable, or has no such field.
    """
    try:
        text = note_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    fm, _body = split_frontmatter(text)
    return _coerce_access_count(fm.get("access_count"))


def seed(
    vault: Path, db_path: Path, *, dry_run: bool = False
) -> dict[str, object]:
    """Reconcile ``note_metrics.access_count`` against vault frontmatter.

    Returns a stats dict suitable for ``print(json.dumps(stats))``.
    """
    if not db_path.exists():
        raise SystemExit(f"cortex-index DB not found: {db_path}")
    if not vault.exists():
        raise SystemExit(f"vault not found: {vault}")

    conn = sqlite3.connect(str(db_path))
    try:
        # Pull slug → path from the indexer's notes table — keeps this
        # script aligned with the indexer's slug-disambiguation logic
        # rather than re-implementing slug_for() here.
        rows = conn.execute("SELECT slug, path FROM notes").fetchall()

        updates: list[tuple[int, str]] = []
        missing_files = 0
        nonzero_count = 0
        max_count = 0
        for slug, rel_path in rows:
            note_path = vault / rel_path
            if not note_path.exists():
                missing_files += 1
            count = _read_access_count(note_path)
            if count > 0:
                nonzero_count += 1
                max_count = max(max_count, count)
            updates.append((count, slug))

        if not dry_run:
            # Upsert: if note_metrics is missing a row (rare — the
            # indexer seeds every slug), insert it; otherwise SET the
            # access_count to the frontmatter value. SET (not +=)
            # gives us idempotency.
            conn.executemany(
                """
                INSERT INTO note_metrics(slug, access_count)
                VALUES(?, ?)
                ON CONFLICT(slug) DO UPDATE SET access_count = excluded.access_count
                """,
                [(slug, count) for (count, slug) in updates],
            )
            conn.commit()
    finally:
        conn.close()

    return {
        "notes_seen": len(rows),
        "missing_files": missing_files,
        "notes_with_nonzero_count": nonzero_count,
        "max_access_count": max_count,
        "dry_run": dry_run,
        "db_path": str(db_path),
        "vault_root": str(vault),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile note_metrics.access_count from vault frontmatter. "
            "Idempotent: re-running SETs (not increments) from the source."
        )
    )
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without modifying the DB.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stats JSON on success.",
    )
    args = parser.parse_args(argv)

    stats = seed(args.vault, args.db, dry_run=args.dry_run)
    if not args.quiet:
        print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
