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
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yaml_lite import extract_wikilinks, split_frontmatter  # noqa: E402


SCHEMA_VERSION = 1

# Belt-and-suspenders frontmatter stripper applied to the raw file text
# before ``split_frontmatter`` runs. ``split_frontmatter`` is line-based
# and bails out on irregular shapes (no leading fence, missing closing
# fence, CRLF endings, etc.); this regex catches the well-formed cases
# unconditionally so frontmatter keys like ``access_count`` and
# ``last_accessed`` never reach the FTS5 corpus.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# Folders whose inbound links count as structural citations.
STRUCTURAL_FOLDERS = {"projects", "reference", "people", "decisions"}
STRUCTURAL_ROOT_FILES = {"index"}  # /index.md at vault root

DEFAULT_VAULT = Path.home() / "alice-mind" / "cortex-memory"
DEFAULT_DB = Path.home() / "alice-mind" / "inner" / "state" / "cortex-index.db"

# Phase 4 (#381): cozylobe-cortex is a SEPARATE vault indexed into a
# SEPARATE DB. The structure of notes is identical (frontmatter +
# wikilinks + markdown body), so the same parsing logic + FTS5 schema
# work — but the two indices must never share rows. Privacy isolation:
# the cue runner queries cortex-index.db by default and never touches
# cozylobe-cortex-index.db unless explicitly pointed at it. See design
# §4.6 of cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md.
COZYLOBE_VAULT_ENV = "COZYLOBE_CORTEX_ROOT"
DEFAULT_COZYLOBE_VAULT = Path.home() / "alice-mind" / "cozylobe-cortex"
DEFAULT_COZYLOBE_DB = (
    Path.home() / "alice-mind" / "inner" / "state" / "cozylobe-cortex-index.db"
)


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


_BUILT_AT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_built_at(raw: str) -> float | None:
    """Parse ``meta.built_at`` ('YYYY-MM-DD HH:MM:SS TZ') into a unix timestamp.

    The timestamp is written by :func:`build` via ``time.strftime('%Y-%m-%d %H:%M:%S %Z')``
    in local time, so the trailing zone name ('EDT', 'EST', 'UTC', …) is
    informational only — Python's stdlib can't reliably round-trip arbitrary
    zone abbreviations. We strip the suffix and treat the timestamp as local
    time, which matches how it was written.

    Returns None on any parse failure so the caller can fall back to rebuilding.
    """
    parts = raw.strip().rsplit(" ", 1)
    head = parts[0] if len(parts) == 2 else raw.strip()
    try:
        return datetime.strptime(head, _BUILT_AT_FORMAT).timestamp()
    except (ValueError, OSError):
        return None


def _meta_built_at_and_count(db_path: Path) -> tuple[float | None, int | None]:
    """Read ``built_at`` (as unix ts) and ``note_count`` from the DB's meta row.

    Returns ``(None, None)`` if the DB is unopenable, missing the meta table,
    or has no meta row. The caller treats a None timestamp as "force rebuild" —
    a corrupt/legacy DB should always rebuild rather than silently stay stale.
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return None, None
    try:
        row = conn.execute("SELECT built_at, note_count FROM meta LIMIT 1").fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()
    if row is None:
        return None, None
    built_at_ts = _parse_built_at(row[0]) if row[0] else None
    note_count = row[1] if row[1] is not None else None
    return built_at_ts, note_count


def needs_rebuild(vault: Path, db_path: Path, max_stale_seconds: int = 86400) -> bool:
    """True if the index DB is stale relative to the vault, or missing.

    We compare ``meta.built_at`` (recorded when the rebuild last completed)
    against ``vault_mtime`` — NOT the DB file's filesystem mtime. SQLite WAL
    mode, query side-effects, and external opens all bump the file mtime
    without the index actually being rebuilt; relying on it produced false
    negatives that hid 6 days of vault drift in June 2026.

    Belt-and-suspenders: if the recorded ``note_count`` doesn't match the
    number of markdown files currently in the vault, force a rebuild. Both
    bugs are guarded — a UNIQUE-constraint crash mid-rebuild leaves an old
    DB in place; the count mismatch catches that on the next check.
    """
    if not db_path.exists():
        return True
    built_at_ts, meta_note_count = _meta_built_at_and_count(db_path)
    # Fail-safe: legacy/corrupt DB with no meta row → rebuild.
    if built_at_ts is None:
        return True
    # Safety bound: rebuild if recorded build is older than max_stale_seconds.
    if (time.time() - built_at_ts) > max_stale_seconds:
        return True
    # ``built_at`` is stored at second resolution via ``time.strftime``; the
    # filesystem mtime carries sub-second precision. Compare at second
    # resolution to avoid spurious rebuilds when the vault was modified in
    # the same second the index was built.
    if int(vault_mtime(vault)) > built_at_ts:
        return True
    # Note count mismatch — vault grew or shrank since last build.
    if meta_note_count is not None:
        try:
            vault_note_count = sum(
                1
                for md in vault.rglob("*.md")
                if not any(part.startswith(".") for part in md.relative_to(vault).parts)
            )
        except OSError:
            return True
        if vault_note_count != meta_note_count:
            return True
    return False


def slug_for(
    path: Path, vault: Path, colliding_stems: frozenset[str] = frozenset()
) -> str:
    """Slug = filename stem; qualified by full relative folder path when stems collide.

    Filenames are typically unique across a vault, so the bare stem suffices
    for the common case. When two notes share a stem (e.g., decisions/_index.md
    and findings/_index.md), the slug becomes "<relative/parent>/<stem>" so the
    UNIQUE constraint on notes.slug holds.

    We use the FULL relative parent — not just the top-level folder — because
    deep collisions are real: archive/dispatched-inflight/README.md and
    archive/refactor-plans/README.md both share top-level folder "archive",
    so a "<top>/<stem>" slug would still collide. Wikilinks resolution falls
    back to alias and title lookups, so the qualified slug doesn't break
    inbound links.

    Root-level files (parent == ".") fall through to the bare stem — there is
    no meaningful folder qualifier at the root, and bare-stem uniqueness is
    enforced anyway by the file system at that level.
    """
    if path.stem in colliding_stems:
        parent = path.relative_to(vault).parent
        if str(parent) not in (".", ""):
            return f"{parent.as_posix()}/{path.stem}"
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
        body = _FRONTMATTER_RE.sub("", body)
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
            "_fm_slug": str(fm.get("slug") or "").strip(),
            "path": str(md.relative_to(vault)),
            "folder": folder_for(md, vault),
            "title": str(title),
            "note_type": str(fm.get("note_type") or ""),
            "status": str(fm.get("status") or ""),
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "aliases_json": json.dumps(aliases, ensure_ascii=False),
            "created": str(fm.get("created") or ""),
            "updated": str(fm.get("updated") or ""),
            "access_count": int(fm.get("access_count") or 0),
            "body": body,
            "_aliases": aliases,
            "_wikilink_targets": extract_wikilinks(body),
            "_path_obj": md,
        }
        records.append(record)
    return records


def build_resolution_maps(
    records: list[dict],
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Build slug → record, alias → record, title → record maps."""
    by_slug: dict[str, dict] = {}
    by_alias: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    for r in records:
        by_slug[r["slug"]] = r
        # A note may be linked by its frontmatter `slug:` when that differs
        # from the filename stem. Register it as a secondary resolution key,
        # but never let it shadow a canonical filename-stem slug (setdefault).
        fm_slug = r.get("_fm_slug")
        if fm_slug and fm_slug != r["slug"]:
            by_slug.setdefault(fm_slug, r)
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

        # Seed note_metrics from frontmatter. Frontmatter is canonical;
        # the cue runner bumps both frontmatter and DB on each retrieval,
        # so reading from frontmatter on rebuild preserves accumulated counts.
        for r in records:
            conn.execute(
                "INSERT INTO note_metrics(slug, access_count) VALUES(?, ?)",
                (r["slug"], r["access_count"]),
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


def _resolve_cozylobe_paths(vault: Path | None, db: Path | None) -> tuple[Path, Path]:
    """Pick the cozylobe-cortex vault + db paths for ``--cozylobe`` runs.

    Precedence:
    1. CLI ``--vault`` / ``--db`` overrides win when explicitly set
       (i.e. not equal to :data:`DEFAULT_VAULT` / :data:`DEFAULT_DB`).
    2. The ``COZYLOBE_CORTEX_ROOT`` env var picks the vault root.
    3. :data:`DEFAULT_COZYLOBE_VAULT` / :data:`DEFAULT_COZYLOBE_DB`.
    """
    import os as _os

    if vault is None or vault == DEFAULT_VAULT:
        env_root = _os.environ.get(COZYLOBE_VAULT_ENV)
        if env_root:
            vault = Path(env_root).expanduser()
        else:
            vault = DEFAULT_COZYLOBE_VAULT
    if db is None or db == DEFAULT_DB:
        db = DEFAULT_COZYLOBE_DB
    return vault, db


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
    parser.add_argument(
        "--cozylobe",
        action="store_true",
        help=(
            "Build the cozylobe-cortex index instead of the main "
            "cortex-memory index. Defaults vault to "
            f"{DEFAULT_COZYLOBE_VAULT} (override via --vault or "
            f"${COZYLOBE_VAULT_ENV}) and db to {DEFAULT_COZYLOBE_DB}."
        ),
    )
    args = parser.parse_args(argv)

    if args.cozylobe:
        # Re-use the same build pipeline against the cozylobe vault.
        # Same frontmatter schema, same FTS5 schema, separate DB file.
        # Privacy isolation is enforced by the cue runner — see design §4.6.
        vault, db = _resolve_cozylobe_paths(args.vault, args.db)
    else:
        vault, db = args.vault, args.db

    if args.check:
        return 0 if needs_rebuild(vault, db) else 1

    stats = build(vault, db)
    if not args.quiet:
        print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
