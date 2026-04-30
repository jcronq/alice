# cortex-index

SQLite + FTS5 index over an Obsidian-style markdown vault. Projects YAML
frontmatter, body text, and `[[wikilinks]]` into a queryable database
that consumers can use for full-text search, link-graph analysis, and
status/tag/folder filters.

## What it is

A derived index over a markdown vault. The vault is canonical; this DB is
projected from it. Wipe the DB, rebuild from the vault, identical state —
by construction. No round-trip writes from DB to vault.

Typical consumers:
- Retrieval — full-text + tag/folder lookup before composing a response.
- Vault hygiene — zero-citation notes, broken-link queue, stale-status
  scans, orphan detection.
- Promotion / TTL — citation-pressure heuristics, eviction candidate lists.

## Usage

```bash
# Default: walk ~/alice-mind/cortex-memory/, write
# ~/alice-mind/inner/state/cortex-index.db
python3 build_index.py

# Quiet mode (skip stats output)
python3 build_index.py --quiet

# Check whether rebuild is needed (exit 0 = stale, exit 1 = fresh)
python3 build_index.py --check && python3 build_index.py

# Override paths
python3 build_index.py --vault /path/to/vault --db /path/to/index.db
```

Stdlib-only: `sqlite3` + a hand-rolled minimal YAML frontmatter parser
(`yaml_lite.py`). No `pyyaml` or `python-frontmatter` dependency, so the
indexer runs in any Python 3.10+ environment without venv ceremony.

## Schema

```
meta(schema_version, built_at, vault_root, note_count)

notes(rowid, slug, path, folder, title, note_type, status,
      tags_json, aliases_json, created, updated, body)
  -- Class A: projected from frontmatter on every rebuild.

links(source_slug, target_slug, target_raw, is_structural, resolved)
  -- All wikilinks. is_structural=1 iff target is in
  -- {projects/, reference/, people/, decisions/, /index.md}.
  -- resolved=0 means unresolved wikilink (repair queue).

note_metrics(slug, access_count, last_queried, speaking_accessed_at)
  -- Class B: operational telemetry. Resets on rebuild — not durable.

notes_fts (FTS5 external-content over notes.title + notes.body)
  -- Full-text search; populated via AFTER INSERT/UPDATE/DELETE triggers.
  -- Contentless table: join via rowid back to notes for slug retrieval.
```

## Atomic rebuild

The indexer writes to `cortex-index.db.tmp`, populates fully, and uses
`os.replace()` to swap it into place. Concurrent readers either see the
old DB or the new DB — never a torn intermediate. Never modify the live
DB in place.

## Wikilink resolution

Order: (1) exact slug match, (2) alias match against frontmatter `aliases:`,
(3) display-title match. Folder-prefixed targets like `[[subdir/foo]]` are
tried as both the full path and the basename. Code blocks (fenced and
inline) are stripped before extraction, so `[[ -d "$x" ]]` (bash) and
`[[wikilinks]]` (markdown examples) don't pollute the broken-link queue.

## Common queries

```sql
-- Stale-finding lint candidates
SELECT slug, updated FROM notes
WHERE status='open' AND updated < date('now', '-7 days');

-- Citation-pressure: structural inbound counts
SELECT target_slug, COUNT(DISTINCT source_slug) AS cites
FROM links WHERE is_structural=1 AND resolved=1
GROUP BY target_slug ORDER BY cites DESC;

-- TTL eviction candidates: zero-citation synthesis older than 30 days
SELECT n.slug FROM notes n
LEFT JOIN (
    SELECT target_slug, COUNT(DISTINCT source_slug) c
    FROM links WHERE is_structural=1 AND resolved=1 GROUP BY target_slug
) lc ON lc.target_slug=n.slug
WHERE n.note_type='synthesis'
  AND n.status NOT IN ('resolved','falsified','superseded','complete')
  AND COALESCE(lc.c, 0) = 0
  AND n.created < date('now', '-30 days');

-- Broken wikilinks (repair queue)
SELECT source_slug, target_raw FROM links WHERE resolved=0;

-- FTS over note bodies and titles
SELECT n.slug, n.title FROM notes n
JOIN notes_fts ON notes_fts.rowid = n.rowid
WHERE notes_fts MATCH 'volume AND bound'
ORDER BY rank LIMIT 5;

-- Backlinks for a target
SELECT source_slug FROM links
WHERE target_slug = 'some-slug' AND resolved = 1;
```

## Trigger discipline

Consumers should `stat()` the vault directory mtime before querying. If
`vault_mtime > db_mtime` → rebuild (or call `--check`). If the DB is
missing or older than 24h → rebuild unconditionally. The `needs_rebuild()`
helper in `build_index.py` encapsulates this.

## What lives elsewhere

- The retrieval protocol (when a consumer should query the index before
  answering) belongs in the consumer's own configuration, not here.
- Eviction / promotion logic belongs in the grooming layer that owns the
  vault — this package only provides the queries.
