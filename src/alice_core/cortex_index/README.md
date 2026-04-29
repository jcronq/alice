# cortex-index

SQLite + FTS5 index over `~/alice-mind/cortex-memory/`. Phase 0 deliverable
of the cortex-signal-architecture design (see
`~/alice-mind/cortex-memory/research/2026-04-28-cortex-signal-architecture.md`).

## What it is

A derived index over the markdown vault. The vault is canonical; this DB is
projected from it. Wipe the DB, rebuild from vault, identical state — by
construction. No round-trip writes from DB to vault.

Used by both hemispheres:
- **Speaking** — proactive vault retrieval (FTS5 + tag/folder filters)
  before composing responses (per [[design-retrieval-protocol]]).
- **Thinking** — Stage B/C queries: zero-citation notes, broken-link queue,
  `status: open AND age > 30d`, etc.
- **Researcher** (Phase 2) — citation-pressure promotion, hypothesis-extraction
  triggers.

## Usage

```bash
# Default: walk ~/alice-mind/cortex-memory/, write inner/state/cortex-index.db
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
  -- resolved=0 means unresolved wikilink (Stage B repair queue).

note_metrics(slug, access_count, last_queried, speaking_accessed_at)
  -- Class B: operational telemetry. Resets on rebuild — not durable.

notes_fts (FTS5 external-content over notes.title + notes.body)
  -- Full-text search; populated via AFTER INSERT/UPDATE/DELETE triggers.
```

## Atomic rebuild

The indexer writes to `cortex-index.db.tmp`, populates fully, and uses
`os.replace()` to swap it into place. Concurrent readers either see the
old DB or the new DB — never a torn intermediate. Never modify the live
DB in place.

## Wikilink resolution

Order: (1) exact slug match, (2) alias match against frontmatter `aliases:`,
(3) display-title match. Folder-prefixed targets like `[[inner/notes/foo]]`
are tried as both the full path and the basename. Code blocks (fenced and
inline) are stripped before extraction, so `[[ -d "$x" ]]` (bash) and
`[[wikilinks]]` (markdown examples) don't pollute the broken-link queue.

## Common queries

```sql
-- Stage B stale-finding lint candidates
SELECT slug, updated FROM notes
WHERE status='open' AND updated < date('now', '-7 days');

-- Citation-pressure promotion: structural inbound counts
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

-- Broken wikilinks (Stage B repair queue)
SELECT source_slug, target_raw FROM links WHERE resolved=0;

-- FTS over note bodies and titles
SELECT n.slug, n.title FROM notes n
JOIN notes_fts ON notes_fts.rowid = n.rowid
WHERE notes_fts MATCH 'volume AND bound';
```

## Trigger discipline

Both hemispheres should `stat()` the vault directory mtime before querying.
If `vault_mtime > db_mtime` → rebuild (or call `--check`). If the DB is
missing or older than 24h → rebuild unconditionally. The `needs_rebuild()`
helper in `build_index.py` encapsulates this.

## What lives elsewhere

- The retrieval protocol (when Speaking should query the index before
  answering) lives in CLAUDE.md / `cortex-memory/reference/design-retrieval-protocol.md`.
- The `query_log` table extension is Phase 3 — added once the retrieval
  habit exists and there's something to observe.
- TTL eviction logic lives in the thinking-bootstrap Stage C op, not here.
  The indexer only provides the queries; thinking decides what to evict.
