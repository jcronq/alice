---
title: Archive Pipeline — Concrete Specs (Schemas + Index Format)
tags: [reference, memory-system, archive, compaction]
created: 2026-04-26
source: atomized-from-2026-04-26-unified-archive-pipeline-design
related: [[2026-04-26-unified-archive-pipeline-design]], [[alice-speaking]], [[design-unified-context-compaction]]
---

# Archive Pipeline — Concrete Specs

Extracted from [[2026-04-26-unified-archive-pipeline-design]]. Design rationale and job descriptions live there; this note contains the concrete schemas and format specs needed for implementation.

---

## Access Log Schema

`inner/archive/access.jsonl` — append-only, one event per archive read:

```json
{
  "ts": "2026-04-26T16:30:00-04:00",
  "topic": "compaction-token-fix",
  "access_type": "functional",
  "trigger": "Owner asked about compaction decision from earlier",
  "turns_read_range": [1745703600.0, 1745706000.0]
}
```

**Fields:**
- `ts` — ISO-8601 with offset. When the read happened.
- `topic` — topic slug from `index.md` row. Must match exactly (this is the join key for scoring).
- `access_type` — `"functional"` (Speaking needed it to answer Owner) or `"incidental"` (read in passing, cross-link).
- `trigger` — free-text, 1 sentence. Why Speaking read the archive. For debugging; not used in scoring.
- `turns_read_range` — `[min_ts, max_ts]` of turns actually read. Not the full topic range; just what was fetched.

**Who writes it:** Speaking, every time she reads from `inner/archive/index.md` or scans `speaking-turns.jsonl` for a past topic. One append per lookup (not per turn read). If Speaking looks up two topics in one interaction, two access events.

**Alternative (v1 bootstrap):** Thinking infers access from outbound content — if Speaking's reply mentions facts from topic X, X was likely read. Noisier but requires no Speaking behavior change. Recommended for v1; upgrade to explicit when Speaking is instrumented.

---

## Index Format

### Standalone: `inner/archive/index.md`

Full table maintained by Thinking. Reference copy — the authoritative index.

```markdown
---
title: Alice Conversation Archive Index
updated: 2026-04-26 18:15
total_topics: N
---

# Archive Index

| Topic | Tags | Description | Turn range (ts) | Approx date | Score | Label | Last accessed | Embedded |
|---|---|---|---|---|---|---|---|---|
| compaction-token-fix | memory-system, design | Fixed should_compact() field bug; agreed harness change | 1745703600–1745706000 | 2026-04-26 | 1.8 | high | 2026-04-26 | ✓ |
| <topic-slug-a> | <domain-tag> | <one-sentence summary> | <ts-start>–<ts-end> | YYYY-MM-DD | 0.6 | medium | — | — |
| <topic-slug-b> | <domain-tag> | <one-sentence summary> | <ts-start>–<ts-end> | YYYY-MM-DD | 0.2 | low | YYYY-MM-DD | — |
```

**Columns:**
- `Topic` — stable slug (kebab-case). Thinking assigns; doesn't change once set.
- `Tags` — domain tags from controlled vocabulary (comma-separated).
- `Description` — 1-sentence summary of what happened in the topic.
- `Turn range (ts)` — unix timestamps of first and last turn (floats, formatted as int).
- `Approx date` — human-readable date of the topic (inferred from `ts`).
- `Score` — computed by Job 3 (importance scoring). Updated weekly.
- `Label` — `high` / `medium` / `low` / `stale` (derived from score).
- `Last accessed` — date of most recent access event. `—` if never accessed.
- `Embedded` — `✓` if this topic appears in the compaction summary §5; blank otherwise.

### Embedded: Compaction summary §5 (Archive pointers)

Condensed, immediately actionable. Appended to `context-summary.md` by Thinking (or by the compaction pass itself, once integrated).

```markdown
## §5 — Archive pointers (top topics by importance)

Use `inner/archive/index.md` to find full list. Scan `inner/state/speaking-turns.jsonl` for turn range.

| Topic | Description | Turn range | Score |
|---|---|---|---|
| compaction-token-fix | Fixed should_compact() field bug | 1745703600–1745706000 | 1.8 |
| <topic-slug-a> | <one-sentence summary> | <ts-start>–<ts-end> | 0.6 |
| unified-archive-design | Full archive pipeline verdicts and design | 1745900000–1745920000 | 1.2 |
```

**Format:** condensed markdown table, top 10 topics with label `medium` or higher, sorted by score descending. No tags, no last_accessed — keeps §5 scannable.

**Who maintains it:** Thinking updates §5 during Stage B grooming when the standalone index changes. Format is stable; no harness change needed for baseline. Future: compaction prompt template could include a `{{archive_pointers}}` slot to automate injection.

---

## Turns Log Schema + Harness Contract

`inner/state/speaking-turns.jsonl` — the cold archive. Harness writes every turn automatically; no Speaking action required.

**Current schema:**
```json
{"ts": 1776995391.0, "sender_number": "+15555550100", "sender_name": "Owner",
 "inbound": "...", "outbound": "...", "error": null}
```

**Schema after harness update (Phase 1, Q5):**
```json
{"ts": 1776995391.0, "sender_number": "+15555550100", "sender_name": "Owner",
 "inbound": "...", "outbound": "...", "thinking": "...", "tools_used": ["Read", "Bash"],
 "error": null}
```

- `thinking` — assistant reasoning steps. Not shown to user; high value for debugging and pattern-mining.
- `tools_used` — list of tool names called during the turn. Payloads NOT stored (too large, low recall value).
- `error` — `null` or error string.

**Harness contract:**
- Every turn written within 100ms of outbound send (or on error).
- `thinking` extracted from SDK `ResultMessage.content` (blocks of type `thinking_delta` / `thinking`).
- `tools_used` extracted from tool-use content blocks; payloads omitted.
- Log is append-only. Harness never truncates, rewrites, or deletes.
- Thinking and Speaking treat the log as read-only from their side.
