#!/usr/bin/env python3
"""Build cortex-index.db from a markdown vault.

Walks the vault root (default: ~/alice-mind/cortex-memory/), parses YAML
frontmatter and wikilinks, populates a SQLite + FTS5 index at the DB path
(default: ~/alice-mind/inner/state/cortex-index.db).

Design constraints:
  - Vault is canonical. DB is a derived index. Wipe DB → rebuild from vault →
    identical state. No round-trip writes from DB to vault.
  - Class A (canonical, projected from frontmatter): notes table.
  - Class B (operational telemetry): note_metrics table; resets on rebuild.
  - Atomic rebuild: write to .tmp → os.replace to final path. Never modify
    the live DB in place.
  - FTS5 external-content over notes table for full-text search.
  - Structural folders: projects/, reference/, people/, decisions/, plus
    index.md at vault root. Links into these folders mark is_structural=1.
  - Wikilink resolution: (1) exact slug match, (2) alias from frontmatter,
    (3) display-title match. Unresolved → resolved=0 (repair queue).

Usage:
    python3 build_index.py                  # rebuild against default paths
    python3 build_index.py --vault PATH     # override vault root
    python3 build_index.py --db PATH        # override output DB path
    python3 build_index.py --check          # exit 0 if rebuild needed, 1 if fresh
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yaml_lite import extract_wikilinks, split_frontmatter  # noqa: E402


SCHEMA_VERSION = 1

# Folders whose inbound links count as structural citations.
STRUCTURAL_FOLDERS = {"projects", "reference", "people", "decisions"}
STRUCTURAL_ROOT_FILES = {"index"}  # /index.md at vault root

DEFAULT_VAULT = Path.home() / "alice-mind" / "cortex-memory"
DEFAULT_DB = Path.home() / "alice-mind" / "inner" / "state" / "cortex-index.db"


SCHEMA_SQL = """
CREATE TABLE meta (
    schema_version INTEGER NOT NULL,
    built_at TEXT NOT NULL,
    vault_root TEXT NOT NULL,
    note_count INTEGER NOT NULL DEFAULT 0
);

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

CREATE INDEX idx_notes_status ON notes(status);
CREATE INDEX idx_notes_type ON notes(note_type);
CREATE INDEX idx_notes_folder ON notes(folder);
CREATE INDEX idx_notes_updated ON notes(updated);

CREATE TABLE links (
    source_slug TEXT NOT NULL,
    target_slug TEXT NOT NULL,
    target_raw TEXT NOT NULL,
    is_structural INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_links_source ON links(source_slug);
CREATE INDEX idx_links_target ON links(target_slug);
CREATE INDEX idx_links_structural ON links(is_structural);
CREATE INDEX idx_links_resolved ON links(resolved);

-- Class B: operational telemetry. Resets on rebuild.
CREATE TABLE note_metrics (
    slug TEXT PRIMARY KEY,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_queried TEXT,
    speaking_accessed_at TEXT
);

-- FTS5 external-content over notes.body
CREATE VIRTUAL TABLE notes_fts USING fts5(
    title, body,
    content='notes',
    content_rowid='rowid',
    tokenize='porter unicode61'
);
"""

FTS_TRIGGERS_SQL = """
CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES('delete', old.rowid, old.title, old.body);
    INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
"""


def vault_mtime(vault: Path) -> float:
    """Maximum mtime over the vault directory + immediate subdirs (cheap)."""
    mtime = vault.stat().st_mtime
    for entry in vault.iterdir():
        if entry.is_dir():
            try:
                mtime = max(mtime, entry.stat().st_mtime)
            except OSError:
                continue
    return mtime


def needs_rebuild(vault: Path, db_path: Path, max_stale_seconds: int = 86400) -> bool:
    if not db_path.exists():
        return True
    db_mtime = db_path.stat().st_mtime
    # Safety bound: rebuild if DB is older than max_stale_seconds regardless.
    if (time.time() - db_mtime) > max_stale_seconds:
        return True
    return vault_mtime(vault) > db_mtime


def slug_for(path: Path, vault: Path, colliding_stems: frozenset[str] = frozenset()) -> str:
    """Slug = filename stem; qualified by folder when stems collide.

    Filenames are typically unique across a vault, so the bare stem suffices
    for the common case. When two notes share a stem (e.g., decisions/_index.md
    and findings/_index.md), the slug becomes "<folder>/<stem>" so the UNIQUE
    constraint on notes.slug holds. Wikilinks usually reference by basename;
    resolution still falls back to alias and title lookups, so the qualified
    slug doesn't break inbound links.
    """
    if path.stem in colliding_stems:
        folder = folder_for(path, vault)
        if folder:
            return f"{folder}/{path.stem}"
    return path.stem


def folder_for(path: Path, vault: Path) -> str:
    rel = path.relative_to(vault).parts
    return rel[0] if len(rel) > 1 else ""


def is_structural_target(target_path: Path, vault: Path) -> bool:
    folder = folder_for(target_path, vault)
    if folder in STRUCTURAL_FOLDERS:
        return True
    if folder == "" and target_path.stem in STRUCTURAL_ROOT_FILES:
        return True
    return False


def collect_notes(vault: Path) -> list[dict]:
    """First pass: parse every note's frontmatter + body, extract wikilinks."""
    # Pre-scan to detect stem collisions so slug_for can fall back to folder/stem.
    # Without this, two notes sharing a filename (e.g., decisions/_index.md and
    # findings/_index.md) would trip the UNIQUE constraint on notes.slug and the
    # rebuild would silently fail.
    paths: list[Path] = []
    stem_counts: dict[str, int] = {}
    for md in vault.rglob("*.md"):
        if any(part.startswith(".") for part in md.relative_to(vault).parts):
            continue
        paths.append(md)
        stem_counts[md.stem] = stem_counts.get(md.stem, 0) + 1
    colliding_stems = frozenset(stem for stem, n in stem_counts.items() if n > 1)

    records = []
    for md in paths:
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        fm, body = split_frontmatter(text)
        slug = slug_for(md, vault, colliding_stems)
        title = fm.get("title") or slug
        if isinstance(title, list):
            title = title[0] if title else slug
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases] if aliases else []
        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [tags] if tags else []
        record = {
            "slug": slug,
            "path": str(md.relative_to(vault)),
            "folder": folder_for(md, vault),
            "title": str(title),
            "note_type": str(fm.get("note_type") or ""),
            "status": str(fm.get("status") or ""),
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "aliases_json": json.dumps(aliases, ensure_ascii=False),
            "created": str(fm.get("created") or ""),
            "updated": str(fm.get("updated") or ""),
            "body": body,
            "_aliases": aliases,
            "_wikilink_targets": extract_wikilinks(body),
            "_path_obj": md,
        }
        records.append(record)
    return records


def build_resolution_maps(records: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Build slug → record, alias → record, title → record maps."""
    by_slug: dict[str, dict] = {}
    by_alias: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    for r in records:
        by_slug[r["slug"]] = r
        # Wikilinks may also reference by basename without folder; same as slug here.
        for alias in r["_aliases"]:
            if isinstance(alias, str) and alias:
                by_alias.setdefault(alias, r)
        title = r["title"]
        if title:
            by_title.setdefault(title, r)
    return by_slug, by_alias, by_title


def resolve_link(
    raw: str,
    by_slug: dict[str, dict],
    by_alias: dict[str, dict],
    by_title: dict[str, dict],
) -> dict | None:
    """Resolve a wikilink target. Order: slug → alias → title.

    `raw` may include a folder prefix (e.g., 'subdir/foo'); we try the full
    path first, then the basename, since vaults often address by basename.
    """
    candidate = raw.strip()
    if not candidate:
        return None
    # Try as-is, then with basename only.
    for key in (candidate, candidate.rsplit("/", 1)[-1]):
        if key in by_slug:
            return by_slug[key]
        if key in by_alias:
            return by_alias[key]
        if key in by_title:
            return by_title[key]
    return None


def build(vault: Path, db_path: Path) -> dict:
    """Rebuild the index. Returns stats dict."""
    if not vault.exists():
        raise SystemExit(f"vault not found: {vault}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    started = time.time()
    records = collect_notes(vault)
    by_slug, by_alias, by_title = build_resolution_maps(records)

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_TRIGGERS_SQL)

        # Insert notes (FTS triggers populate notes_fts automatically).
        for idx, r in enumerate(records, start=1):
            conn.execute(
                """
                INSERT INTO notes(rowid, slug, path, folder, title, note_type, status,
                                  tags_json, aliases_json, created, updated, body)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idx,
                    r["slug"],
                    r["path"],
                    r["folder"],
                    r["title"],
                    r["note_type"],
                    r["status"],
                    r["tags_json"],
                    r["aliases_json"],
                    r["created"],
                    r["updated"],
                    r["body"],
                ),
            )
            r["rowid"] = idx

        # Resolve links and insert.
        unresolved_count = 0
        link_count = 0
        for r in records:
            seen: set[tuple[str, str]] = set()
            for raw in r["_wikilink_targets"]:
                target_record = resolve_link(raw, by_slug, by_alias, by_title)
                if target_record is None:
                    target_slug = raw.rsplit("/", 1)[-1]
                    is_structural = 0
                    resolved = 0
                    unresolved_count += 1
                else:
                    target_slug = target_record["slug"]
                    is_structural = (
                        1
                        if is_structural_target(target_record["_path_obj"], vault)
                        else 0
                    )
                    resolved = 1
                key = (target_slug, raw)
                if key in seen:
                    continue
                seen.add(key)
                conn.execute(
                    """
                    INSERT INTO links(source_slug, target_slug, target_raw,
                                      is_structural, resolved)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (r["slug"], target_slug, raw, is_structural, resolved),
                )
                link_count += 1

        # Seed empty note_metrics rows so retrieval-protocol updates don't need INSERTs.
        for r in records:
            conn.execute(
                "INSERT INTO note_metrics(slug, access_count) VALUES(?, 0)",
                (r["slug"],),
            )

        conn.execute(
            "INSERT INTO meta(schema_version, built_at, vault_root, note_count) VALUES(?, ?, ?, ?)",
            (
                SCHEMA_VERSION,
                time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                str(vault),
                len(records),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Atomic swap: rename .tmp into place.
    os.replace(str(tmp_path), str(db_path))

    elapsed = time.time() - started
    return {
        "notes": len(records),
        "links": link_count,
        "unresolved_links": unresolved_count,
        "elapsed_seconds": round(elapsed, 3),
        "db_path": str(db_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 if rebuild needed, 1 if index is fresh",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stats output on success",
    )
    args = parser.parse_args(argv)

    if args.check:
        return 0 if needs_rebuild(args.vault, args.db) else 1

    stats = build(args.vault, args.db)
    if not args.quiet:
        print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
