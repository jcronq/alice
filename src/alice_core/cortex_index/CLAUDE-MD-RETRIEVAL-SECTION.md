# DRAFT v2 — CLAUDE.md "Speaking-side vault retrieval" section

Phase 0 deliverable (a) of the cortex-signal-architecture, revised under
the retrieval-cue model landed 2026-04-28. **Status: proposal. Not yet
pasted into CLAUDE.md** — awaiting Jason's review.

Source: §7 "Retrieval cue architecture" of
`2026-04-28-cortex-signal-architecture.md`. Supersedes draft v1 which
told Speaking to query the DB directly with SQL — under the cue model
the DB query is delegated to a Haiku-tier cue runner that fires
automatically; Speaking reads the packet, not the DB, on the primary
path.

Insertion point: CLAUDE.md "Memory Protocol" section, as a new subsection
under "Storage tiers (all managed by thinking)" titled
"Speaking-side vault retrieval".

---

## Speaking-side vault retrieval

The vault doesn't help Speaking unless Speaking *reads* it before answering.
Model knowledge is no substitute for prior validated reasoning that already
lives in `cortex-memory/`. A retrieval cue runner handles the search
automatically; this section says how to consume the cue and when to
follow up.

### How retrieval works (primary path)

A Haiku-tier cue runner fires on every turn. It takes the user's question
plus a slice of recent conversation context, queries `cortex-index.db`
(SQLite + FTS5), and returns a small **reference packet** of the top
3–5 matches. Each entry contains the slug, title, a relevance score,
and the **specific lines** from the note that matched. The packet
arrives as part of the turn's input; Speaking does not query the DB
directly on the primary path.

Reference packet shape (per candidate):

```yaml
slug: research/foo
title: "..."
score: 0.87
matched_lines:
  - {n: 3, text: "..."}
  - {n: 4, text: "..."}
  - {n: 5, text: "..."}
why_relevant: "..."   # optional one-line summary
```

Per-candidate cap: 5 lines. Packet-level token ceiling: ~1000 tokens.
References whose slug is already named in the active conversation
context are dropped before the packet reaches Speaking (dedup).

### How to consume the packet

Three response patterns, by cost:

1. **Cite the matched lines directly** (cheapest, most turns).
   The lines are usually enough. Quote them with line-level provenance:
   "The vault says X (`cortex-memory/research/foo.md:5`)."
   No Read call needed. Source citations make Speaking's claims
   verifiable and pressure thinking to keep notes accurate.

2. **Follow the reference with a Read call** (when the lines need
   broader context). Use `Read` with `offset`/`limit` to pull a tighter
   window around the matched lines, or read the whole note if the
   answer requires it. This updates `speaking_accessed_at` in the DB
   (telemetry only, never written back to frontmatter).

3. **Ignore the packet** (when nothing matches the question). Answer
   from model knowledge as before. Empty packets are routine and not
   a signal of failure.

If the packet is empty AND the question is one where vault content
would help, fall back to `Grep` over `~/alice-mind/cortex-memory/`
for the keyword. The cue runner is best-effort, not exhaustive.

### When to expect a useful packet

Documentation, not directive — the cue runner fires regardless. This
table describes the question shapes where vault content is likely to
help. If the packet is non-empty on a turn matching one of these
shapes, the matched lines should usually go in the response.

| Question shape | Likely entry points |
|----------------|---------------------|
| Project — CozyHem, alice-viewer, fitness, ripped-by-40 | `projects/<slug>.md` and recent synthesis backlinks |
| Person — Katie, Jason's health | `people/<slug>.md` |
| Past event — "remember when…" | `memory/events.jsonl` (separate from the cue runner; grep directly) |
| Fitness — workout, weight, nutrition | `memory/fitness/CURRENT-WEIGHTS.md`, `projects/fitness.md` |
| Design decision — "how does X work, what did we decide" | `reference/`, `decisions/` |
| Wikilink reference — Jason names `[[X]]` or "design-Y" | Slug/alias resolution; cue runner returns it directly |
| Technical question Alice has researched | `research/` synthesis notes |

If the question is conversational ("how are you", "thanks"), the cue
runner returns an empty packet and Speaking ignores it.

### Vault-contradicts-model

If a matched line contradicts model knowledge, the vault wins —
but file a contradiction-candidate note via `append_note(tag='conflict-candidate')`
so thinking can resolve it. Don't silently override vault content
with model knowledge, and don't silently override model knowledge
with stale vault content; flag the gap.

### Direct DB queries (advanced)

On rare turns where the cue runner missed and Speaking needs more
than `Grep` can express, the indexer at `~/alice-tools/cortex-index/build_index.py`
produces `~/alice-mind/inner/state/cortex-index.db`. Query directly:

```sql
-- Topic search (FTS5):
SELECT n.slug, n.title, n.folder, n.status FROM notes n
JOIN notes_fts ON notes_fts.rowid = n.rowid
WHERE notes_fts MATCH 'cozyhem AND websocket'
ORDER BY rank LIMIT 5;

-- Slug/alias resolution:
SELECT slug, path FROM notes
WHERE slug = 'design-thinking-capabilities'
   OR aliases_json LIKE '%design-thinking-capabilities%';

-- Backlinks:
SELECT source_slug FROM links
WHERE target_slug = 'jason' AND resolved = 1;
```

Before querying, check `vault_mtime > db_mtime`; if so, run
`python3 ~/alice-tools/cortex-index/build_index.py --check && \
python3 ~/alice-tools/cortex-index/build_index.py`. The `--check` flag
exits 0 only when a rebuild is needed.

If the DB is unreachable, fall back to `Grep` and file a note via
`append_note(tag='infra-degraded')` so thinking surfaces the
rebuild as a priority.

### What this isn't

- **Not exhaustive retrieval.** Trust the packet's top 3–5 hits;
  don't read every backlink. Retrieval cost should be measured in
  seconds, not minutes.
- **Not semantic search.** FTS5 + tags + aliases at current vault
  scale (~500 notes). Embedding-based retrieval is out of scope
  until the vault outgrows lexical match.
- **Not a substitute for judgment.** Speaking decides what (if
  anything) from the packet ends up in the response. The cue is
  a nudge, not a requirement.

---

## Open questions before paste

1. **Section placement.** Inserting under "Storage tiers" in
   "Memory Protocol." Alternative: standalone top-level "Vault
   Retrieval" section. Recommendation: keep under Memory Protocol —
   the retrieval-side complement sits next to the write-side rules.
2. **Cue runner availability.** This section assumes the Phase 0 (c)
   Haiku cue runner is shipped. Until then, Speaking has no packet
   on incoming turns. Two options: (a) paste this section now,
   accepting that the "primary path" is dormant until (c) lands, or
   (b) wait and paste both at once. Recommendation: paste now —
   the "Direct DB queries" subsection covers the interim and the
   primary-path description sets the eventual contract.
3. **Trigger table granularity.** Seven rows. Keep tight, or expand
   with sub-cases? Recommendation: keep at seven; sub-cases bloat
   without adding precision.

Resolve these and the section is paste-ready.
