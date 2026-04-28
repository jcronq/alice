---
title: "Cortex-index implementation reference"
aliases: [cortex-index, build-index, cortex-indexer]
tags: [reference, alice-architecture, memory-design]
note_type: reference
status: complete
created: 2026-04-28
related:
  - "[[2026-04-28-cortex-signal-architecture]]"
  - "[[2026-04-26-vault-retrieval-design]]"
  - "[[design-retrieval-protocol]]"
---

# Cortex-index implementation reference

> **tl;dr** SQLite + FTS5 indexer for `cortex-memory/`. Stdlib-only Python (PEP 668 constraint). Atomic rebuild via `.tmp + os.replace`. Located at `alice-tools/cortex-index/`. DB lives at `inner/state/cortex-index.db`. First build: 487 notes, 3496 wikilinks, 7 unresolved, 1.8s.

## Location

- **Script:** `alice-tools/cortex-index/build_index.py`
- **YAML helper:** `alice-tools/cortex-index/yaml_lite.py`
- **DB output:** `~/alice-mind/inner/state/cortex-index.db`

## Design constraints

**Stdlib-only.** No `pyyaml`, no `python-frontmatter`. Host Python is externally-managed (PEP 668 — Docker image uses distro Python); adding packages requires venv ceremony that would complicate both hemispheres invoking the script. `yaml_lite.py` handles frontmatter parsing with a minimal hand-rolled implementation.

**yaml_lite.py** strips fenced code blocks (```` ``` ``` ```) and inline backtick expressions from note bodies before extracting `[[wikilinks]]`. Without this, bash code in design notes produces hundreds of false-positive `[[` matches — this was the source of the 85→7 broken-link reduction at first-build debug.

## Rebuild logic

**`needs_rebuild()` helper** compares vault directory mtime (`cortex-memory/`) to `cortex-index.db` mtime. Single `stat()` syscall — no file walk. Returns `True` if vault is newer.

**24h safety bound:** if `cortex-index.db` is absent or its mtime is >24h old, rebuild unconditionally. Covers deploys and extended idle gaps.

**Atomic write:** builds to `cortex-index.db.tmp`, fully populates, then `os.replace()` → `cortex-index.db`. Prevents torn reads if a hemisphere queries during a slow rebuild.

## Schema highlights

- `notes(slug, path, note_type, status, tags_json, created, updated, body)` — Class A fields from frontmatter; `body` for FTS
- `links(source_slug, target_slug, is_structural, resolved)` — wikilinks; `is_structural=1` if target is in `projects/ | reference/ | people/ | decisions/ | index.md`; `resolved=0` for broken links
- `note_metrics(slug, access_count, last_queried, speaking_accessed_at)` — Class B telemetry, resets on rebuild
- `notes_fts` — FTS5 external-content table on `body`
- `meta(schema_version, built_at)` — version row

**note_metrics pre-seeding:** after populating `notes`, the indexer inserts one empty `note_metrics` row per note slug. This ensures Speaking's retrieval protocol can always `UPDATE note_metrics SET ... WHERE slug = ?` without checking for row existence — no INSERT-or-UPDATE logic needed at query time.

## Initial build baseline (2026-04-28 08:22 EDT)

| Metric | Value |
|--------|-------|
| Notes indexed | 487 |
| Total wikilinks | 3496 |
| Unresolved wikilinks | 7 (typos + inner/notes/.consumed/ refs in arch doc footers) |
| Rebuild time | ~1.8s |

Folder distribution at first build:

| Folder | Count |
|--------|-------|
| research/ | 363 |
| dailies/ | 46 |
| reference/ | 45 |
| feedback/ | 15 |
| projects/ | 9 |
| sources/ | 4 |
| root (vault root) | 3 |
| people/ | 2 |

## Top structural inbound hubs

Computed from `SELECT target_slug, COUNT(*) FROM links GROUP BY target_slug ORDER BY COUNT(*) DESC` (all links, not structural-only). Hubs are typically the owner's principal note, the project notes for active systems, and broad-domain hubs (one per recurring topic). Counts are useful to spot over-linking (a single hub bridging unrelated lobes is a signal that the vault has collapsed into a hairball).

## Rebuild trigger coverage (confirmed 2026-04-28)

`vault_mtime()` detects staleness by scanning the vault root + all immediate subdirectory mtimes. This catches **file creation** correctly (new synthesis notes, new research notes, daily note creation). It does **not** catch pure-modification operations (Stage B frontmatter edits, daily note appends) because Linux directory mtime only updates on directory-entry changes, not file-content changes. 24h safety bound covers worst-case modification-only nights. Option C fix: lower `max_stale_seconds` to 3600 for tighter coverage. Full analysis: [[2026-04-28-fts5-index-staleness-rebuild-coverage]].

## Status at Phase 1 start (pre-normalization, logged for reference)

At first-build time: 388/487 notes had empty `status:` field, 484/487 had empty `note_type:`. Phase 1 normalization sweep (2026-04-28 08:22 EDT) brought all 363 `research/` notes to canonical status. Full frontmatter normalization of `note_type` across all 487 notes is a remaining Phase 1 task.

Some `status:` fields contain multiline prose leakage (e.g., a SUPERSEDED note body fragment, a HUE_API_KEY redaction note). These are edge cases the indexer's frontmatter parser handles by taking only the first non-empty value line — Stage B lint will catch genuinely malformed notes as they surface in queries.
