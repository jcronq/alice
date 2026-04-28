---
title: Design — vault retrieval protocol
aliases: [retrieval-protocol, vault-retrieval, when-to-grep]
tags: [reference, design]
created: 2026-04-26
---

# Vault retrieval protocol

> **tl;dr** Speaking has Grep/Read/Glob but rarely uses them on `cortex-memory/` — answering instead from model knowledge or context-window state. The survey calls this the "no retrieval trigger" failure mode. This note defines *when* Speaking should search the vault before composing a response. No infrastructure; just convention.

## The problem

A mature vault has dozens to hundreds of atomic notes covering project internals, Alice architecture, recurring domains, design decisions, research artifacts. Speaking has the tools to grep them but mostly doesn't unless something specific cues it. Result: answers are weaker than they should be on topics the vault covers thoroughly.

Worst-case retrievals Speaking can fall into:
- Answer an architecture question from pre-trained knowledge instead of `cortex-memory/projects/<system>.md`
- Reason about a project bug without reading the project's "Live bugs" section
- Discuss memory layout from rough mental model instead of `cortex-memory/reference/memory-layout.md`

## The fix

Speaking adopts trigger-based retrieval. Not blanket grep-everything (token cost), but specific cues that should reflexively spawn a Read or Grep before composing.

## Trigger table

| Question shape | First retrieval action |
|---|---|
| "Tell me about [project]" / project status / project bug | Read `cortex-memory/projects/<project>.md` → follow research/ backlinks |
| "How does [system] work" / technical mechanism question | Grep `cortex-memory/reference/` by alias; then `cortex-memory/research/` for recent investigations |
| Person question (Friend, Owner health, who-said-what) | Read `cortex-memory/people/<name>.md` |
| "When did [X] happen" / "have I [...] recently" | Grep `memory/events.jsonl` |
| Domain-specific operational question (per-domain runtime data) | Read `memory/<domain>/<file>.md` + `cortex-memory/projects/<domain>.md` |
| "Why did we [decision]" / design rationale | Grep `cortex-memory/reference/design-*.md` by topic |
| "Is there a known issue with [X]" | Grep both `cortex-memory/projects/` (for "Live bugs" sections) and `cortex-memory/research/` |
| Vault-coverage check before claiming "I don't know" | Glob `cortex-memory/**/*.md` for the keyword |
| Session just compacted OR `turns_since_compaction > 30` | Re-read the active project note(s) for whatever Owner is working on; the compaction just discarded details the vault still holds |

The last two rows interact: **after compaction, do the vault check first** — it replenishes domain knowledge that was just compressed away. Compaction drops context freshness sharply; the vault is the only place that information persists losslessly. See [[2026-04-26-three-tier-information-priority]] for the underlying model.

The "I don't know" row is the most important outside of compaction: **before saying "I don't know" about a topic Alice plausibly has notes on, do a Glob first.** Many of Speaking's "I don't know" answers today were actually answerable from the vault.

## What this is NOT

- **Not blanket retrieval.** Trivial conversational turns ("got it", "thanks", "what time is it") don't trigger. The triggers are content-type-specific.
- **Not a replacement for context-window memory.** When Owner just told me X two turns ago, no need to grep the vault for X — context wins for recent state.
- **Not a substitute for tool delegation.** When the answer requires running a probe, dispatching a worker, or hitting an API, the vault retrieval is preparation for that, not a replacement.

## Cost discipline

Each retrieval is tool calls + context tokens. Triggers above are intentionally narrow. If a turn requires more than ~3 retrievals, that's a sign to either (a) compose with what's gathered and follow up if needed, or (b) note in the reply that deeper investigation would require dispatching a worker.

## Companion mechanisms

*(Designed 2026-04-26 — see [[2026-04-26-retrieval-protocol-extensions]] for full spec.)*

### 1. `speaking_accessed_at` frontmatter field

Lets Thinking see which notes Speaking actually retrieves in turns (not just which Thinking has groomed). Feeds archival decisions: notes Speaking never reads are stronger archival candidates.

**Implementation:** Extend `read_memory` in `tools/memory.py` to also write `speaking_accessed_at: YYYY-MM-DD` alongside the existing `last_accessed` update. ~2 lines. No new tool required.

**Preference rule:** Speaking should prefer `read_memory` over plain `Read` when accessing vault notes (`cortex-memory/**/*.md`). Plain `Read` is fine for non-vault paths. This is the behavioral rule that makes the field useful.

**Archival impact:** Notes with `speaking_accessed_at` within 60 days → resist archival even if `access_count` is low.

### 2. `priority: context` surface type

Thinking drops lightweight vault-pointer hints into `inner/surface/` when inbox drain reveals deep vault coverage on a topic Speaking is about to work. **Not user-facing** — Speaking reads the hint as a retrieval primer and resolves immediately.

**Trigger:** On drain, if a note touches topic X and vault has ≥2 non-obvious research notes on X not likely in Speaking's active session context → emit one context surface. Budget: at most one context surface per wake.

**Format:**
```yaml
---
priority: context
topic: <keyword>
expires_at: <tomorrow YYYY-MM-DD>
context: why this is relevant now
reply_expected: false
---

- `cortex-memory/<path>` — one-line relevance note
- `cortex-memory/<path>` — one-line relevance note

Read these before composing a response if Owner asks about <topic>.
```

**Consumption:** Speaking reads, integrates vault paths as retrieval hints, resolves with `action_taken: "integrated retrieval hints — topic: X"`. If `expires_at` is past, skip body and resolve expired. Never relay to Owner.

**Divergence from flash/insight:** context surfaces are resolved on read (not on action), have 24h TTL, and produce no Signal output.

The trigger table above is the load-bearing change; these two mechanisms amplify it.

## Adoption

Speaking commits to the trigger table starting 2026-04-26 (this note's `created` date). The protocol is mental, not enforced — but it's documented, so deviation is visible: a turn where Speaking obviously should have read the project note and didn't is now a calibration failure that can be flagged in feedback.

## Companion pattern: Reconciliation sweeps

The retrieval protocol governs Speaking's *on-demand* reads. A complementary mechanism covers *ground-truth drift*: when facts accumulate in the speaking-turns log that never flowed through `inner/notes/` into the vault, a periodic reconciliation sweep re-syncs the formal model.

**Canonical structure** (applies to any formal-model/ground-truth sync — vault vs. turns log, a calibrated protocol's formal model vs. live activity, etc.):
1. Gate on new data (stamp file in `inner/state/<feature>-last-ts.txt`)
2. Read ground truth since last stamp
3. **Classify each delta**: new fact (write) / already-captured duplicate (skip) / methodological artifact (flag, don't fix)
4. Write only class-1 deltas to formal model
5. Update stamp

The delta-classification step (3) is where sweeps most often fail: without it, already-captured facts are duplicated and measurement artifacts are incorrectly promoted as new findings. See [[2026-04-27-reconciliation-sweep-delta-classification]] for the full design and [[2026-04-26-speaking-log-mining-design]] for the nightly turns-log sweep that canonicalized this structure.

## Related

- [[llm-agent-memory-survey-2025]] — defines "no retrieval trigger" as a known failure mode
- [[memory-layout]] — vault structure this protocol exploits
- [[memory-types]] — taxonomy of folders the trigger table maps over
- [[design-write-path]] — the corollary protocol for Speaking → Thinking notes
- [[alice-speaking]] — the runtime that adopts this convention
- [[2026-04-26-retrieval-protocol-extensions]] — detailed design for the companion mechanisms (context surfaces + speaking_accessed_at)
- [[2026-04-26-vault-retrieval-design]] — research sketch that preceded this design doc; gap analysis showing retrieval is a protocol problem, not an infrastructure problem
- [[2026-04-26-three-tier-information-priority]] — synthesis showing retrieval urgency and compaction urgency share the same counter (`turns_since_compaction`); also establishes the SP-tier structure across compaction, retrieval, and archival
- [[2026-04-27-reconciliation-sweep-delta-classification]] — canonical reconciliation sweep design; generalizes the speaking-log nightly sweep to any formal-model/ground-truth sync
