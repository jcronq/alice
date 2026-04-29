# Haiku Retrieval Cue Runner — Phase 0 (c) Spec

Phase 0 deliverable (c) of the cortex-signal-architecture (per §7
"Retrieval cue architecture" of `2026-04-28-cortex-signal-architecture.md`).
Status: design spec, awaiting Jason's review before build.

## What it does

Before Speaking composes a response, fire a Haiku-tier process that:
1. Takes the inbound user message + a slice of recent conversation context
2. Queries `~/alice-mind/inner/state/cortex-index.db` (SQLite + FTS5)
3. Returns a small reference packet — top 3–5 candidate notes, each with
   slug, title, score, and the matched lines from the note
4. Drops candidates whose slug is already named in the conversation
5. Prepends the packet to Speaking's prompt as a preamble

Speaking then has the option to cite matched lines directly, follow up
with a Read call for broader context, or ignore the packet entirely.
Token-bounded (~1000 tokens ceiling), dedup'd, agency-preserving.

## Where it lives

**Integration point:** `~/alice/src/alice_speaking/daemon.py`,
function `_compose_prompt()` (around line 1528). This function already
implements the preamble pattern — it prepends bootstrap/compaction
summaries before the turn prompt. The cue runner adds a new preamble
stage that produces and prepends the reference packet.

**Code location for the runner itself:** new module
`~/alice/src/alice_speaking/retrieval/cue_runner.py`. Single async
function `build_cue_packet(user_query, conv_context) → str | None`
returning either the formatted preamble text or `None` for empty
packets / failures.

**Why preamble injection (not MCP tool):** Speaking shouldn't have to
*decide* to run retrieval — automation is the whole point. MCP tools
require Speaking to call them, which gets skipped on routine turns.
Preamble injection guarantees the packet is always present when
non-empty, with zero Speaking-side discipline required.

## Pipeline

```
inbound turn
    ↓
[1] vault freshness check
    stat cortex-memory/ dir mtime; if > db_mtime, run indexer rebuild
    ↓
[2] query construction
    deterministic path: extract keywords from user_query (stopword filter,
    quoted phrase preservation); build FTS5 MATCH expression
    ↓
[3] FTS5 query
    SELECT slug, title, snippet, offsets FROM notes_fts MATCH ? ORDER BY rank LIMIT 8
    ↓
[4] offset → line number mapping
    for each hit, map FTS5 offsets() to line numbers in note body;
    extract whole lines (not phrase snippets) per line-granularity rule
    ↓
[5] decision: deterministic vs Haiku-assisted
    if FTS returned ≥3 strong-rank hits → deterministic, skip Haiku
    if FTS returned 0 hits OR all weak-rank → Haiku-assisted reformulation
    if FTS returned 1-2 hits → use them as-is, no Haiku
    ↓
[6] (Haiku-assisted only) reranking
    send user_query + top-8 candidates (slug+title+matched_lines) to Haiku
    Haiku returns ranked subset of 0–5 with optional why_relevant
    ↓
[7] dedup by slug
    drop candidates whose slug appears as a wikilink in conv_context
    ↓
[8] format as preamble
    YAML-style block of slug/title/score/matched_lines/why_relevant
    cap: 5 candidates × 5 lines, ~1000 token packet ceiling
    ↓
[9] prepend to prompt
    return preamble text; _compose_prompt concatenates before turn body
```

## Packet shape (rendered)

```
## Vault references
Top matches from cortex-memory for this turn. Cite by slug:line.
Use Read for more context if the matched lines don't suffice.

- slug: research/2026-04-26-vault-retrieval-design
  title: "Vault retrieval design"
  score: 0.91
  matched_lines:
    - 12: "Speaking has Grep and Read already; the gap is a protocol spec"
    - 13: "for when Speaking should query cortex before generating."
    - 27: "Trigger: Jason asks about a project — read projects/<slug>.md"
  why_relevant: "Direct match on retrieval protocol question"

- slug: ...
```

Empty case (no hits or all dedup'd):

```
## Vault references
(no relevant vault content for this turn)
```

Empty packets are routine and not surfaced to Jason in any way.
They serve as a positive "we checked" signal in transcripts.

## Decision: when to skip Haiku

**Skip Haiku (deterministic FTS path) when:**
- FTS returns ≥3 hits with rank ≥ threshold (tunable; start at top quartile)
- User query contains an explicit slug or wikilink reference (`[[X]]`)
- User query is keyword-rich (≥2 noun phrases match note titles)

**Use Haiku reranking when:**
- FTS returns 0 hits (semantic reformulation may help)
- All hits are weak-rank (likely false positives)
- User query is conversational phrasing without strong keywords

This gates Haiku invocations to the cases that actually need it.
Most retrieval-eligible turns hit the deterministic path. Estimated
Haiku invocation rate: 20–30% of retrieval-eligible turns.

## Latency budget

| Path | Target |
|------|--------|
| Vault freshness check + FTS query | <50ms |
| Deterministic path total | <100ms |
| Haiku reranking call | <600ms |
| Haiku-assisted path total | <800ms |

Latency is added to the turn before Speaking's first token. The
deterministic path is essentially free; the Haiku path adds noticeable
but acceptable lag on the minority of turns where it fires.

## Anthropic SDK integration

Daemon does not currently import `anthropic` (uses claude-agent-sdk
for Sonnet calls). Add `import anthropic` to daemon.py top-level;
instantiate `Anthropic()` client in `Daemon.__init__()` reading
auth from existing config.

Haiku call shape:
```python
response = await asyncio.to_thread(
    client.messages.create,
    model="claude-haiku-4-7",
    max_tokens=400,
    system=CUE_RUNNER_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": user_msg + candidates_block}],
)
```

CUE_RUNNER_SYSTEM_PROMPT is the bounded job description: "rerank
the candidates by relevance to the user query; return JSON list of
{slug, score, why_relevant} for the top N matches; return empty list
if nothing is relevant; do not invent slugs not in the candidates."

Few-shot examples in the system prompt: 2-3 query/candidates/output
triples covering the common cases (project query, design question,
ambiguous query).

## Failure modes

| Failure | Behavior |
|---------|----------|
| `cortex-index.db` missing or unreadable | Log warning, return `None` (no preamble), skill `append_note(tag='infra-degraded')` from a separate path |
| Vault dir doesn't exist | Same as above |
| Indexer rebuild fails | Use stale DB, log warning |
| FTS query syntax error | Catch, return empty packet |
| Haiku call timeout (>2s) | Fall back to deterministic path top-3 |
| Haiku returns malformed JSON | Fall back to deterministic path |
| Haiku returns slugs not in candidates | Drop hallucinated entries; use rest |
| Conv context missing | Skip dedup; otherwise proceed |

The cue runner must never block or fail the turn. Worst case is an
empty packet, which Speaking handles gracefully (per CLAUDE.md
retrieval section).

## Telemetry (Phase 3 hook)

Phase 3 adds a `query_log` table to the indexer schema. The cue runner
should be ready to write a row per invocation:

```sql
INSERT INTO query_log (ts, query_type, caller, result_count, latency_ms, used_haiku)
VALUES (...)
```

Plumb the call site even before Phase 3 — it can no-op until the
table exists. Once Phase 3 lands, the cue runner is the primary
data source for "is retrieval actually happening" health checks.

## Build estimate

| Task | Time |
|------|------|
| Module scaffold + tests | 2h |
| FTS query construction + offset→line extraction | 3h |
| Deterministic decision logic | 1h |
| Haiku call + system prompt + few-shot | 2h |
| Dedup logic | 1h |
| Preamble formatter | 1h |
| Daemon integration (`_compose_prompt` wiring) | 2h |
| End-to-end test in dev | 2h |

**~14 hours total, ~2 working days.**

Dependencies: cortex-index.db (DONE, Phase 0 (b)), Anthropic SDK
auth config in daemon (likely already present, needs verification).

## Open questions before build

1. **Auth source for Haiku calls.** Daemon currently uses claude-agent-sdk
   which manages auth via OAuth token in `~/.config/anthropic/`. Does
   the same token work for raw `anthropic.Anthropic()` calls, or do we
   need a separate API key in `alice.config.json`? Verify before build.

2. **Rate limit on Haiku.** If retrieval fires every turn and 20–30%
   trigger Haiku, that's tens of Haiku calls per active hour. Within
   rate limits at current Signal traffic, but worth checking the org's
   Haiku TPM/RPM ceilings and adding a circuit breaker.

3. **`alice-cli` and other Speaking transports.** This spec covers the
   Signal path. The cue runner should fire on every Speaking turn
   regardless of transport — verify `_compose_prompt()` is reached by
   CLI invocations as well, or generalize the integration point.

4. **Dev rollout.** Recommend a feature flag (`alice.config.json`:
   `speaking.cue_runner.enabled`) defaulting to `false`. Enable in
   dev, run for 24h to gather latency + packet quality data, then
   enable in production. Avoids a regression if the runner mis-ranks
   on real traffic.
