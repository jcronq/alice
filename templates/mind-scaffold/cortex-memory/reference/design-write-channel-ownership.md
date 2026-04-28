---
title: "Write-channel ownership: a design principle for reliable memory structures"
aliases: [write-channel-ownership, channel-ownership, write-semantics-alignment]
tags: [reference, design, memory-design, reliability]
created: 2026-04-26
---

# Write-channel ownership: a design principle for reliable memory structures

> **tl;dr** A memory structure is reliable for decision-making if and only if the process that *writes* to it matches the semantic intent the *reader* needs. When they match, designs hold under pressure. When they don't, the structure fails in one of three characteristic modes: scope error, contamination, or semantic drift. The single constructive test: "does the process that writes this field match who should own it?"

---

## The positive principle

**Write-channel ownership**: for any field used in downstream decisions, the process that writes to it must be the same process whose output the reader actually needs to measure.

This sounds obvious, but it fails repeatedly in practice because write access is often incidental — a process touches a field as a side effect of doing its primary job, without intending to be the authoritative signal producer.

---

## The failure modes as ownership violations

All three failure modes in [[2026-04-26-metric-failure-taxonomy]] reduce to ownership violations:

### Mode 1 — Scope error (wrong field, correct writer)
`should_compact()` read `input_tokens` (7–23 tokens per Signal turn) instead of `effective_tokens` (150K–200K). The field *was* owned by the right process (the SDK usage tracking), but it was the *wrong field* — a mis-specified ownership claim. The writer was correct; the spec was wrong about which signal to read.

**Ownership diagnosis**: specification failure. The write-channel was correctly owned; the reader specified the wrong channel.

### Mode 2 — Contamination (correct field, wrong writer has access)
Vault grooming reads notes (to check links, fix frontmatter) and writes `last_accessed`. If `last_accessed` is later scored for archival resistance, grooming inflates resistance for everything it touched. The process that *should* own `last_accessed` for archival purposes is Speaking — "did a real conversation need this note recently?" Instead, grooming has incidental write access and contaminates the signal.

**Ownership diagnosis**: shared ownership where exclusive ownership was required. The contaminating process isn't malicious — it simply has write access as a side effect of doing its own job.

### Mode 3 — Semantic drift (correct field, ownership transferred to wrong process)
§1 Active threads in context-summary start as precise deferred actions ("once gog is fixed, land the compose bind-mount + alice.env entries, then audit events.jsonl for gaps"). After N compaction cycles, the record reads "gog integration pending." The compaction LLM *has ownership* of §1 — it rewrites it every cycle — but its semantic goal (terse summary) doesn't match the reader's need (actionable precision). Ownership transferred to a process with incompatible output semantics.

**Ownership diagnosis**: ownership held by a process with wrong semantic intent. The writer is accurate by its own standards; the reader needs a different standard.

---

## The success patterns as ownership alignment

Every data structure in Alice's memory that has required no patches shares the same property: the write semantics exactly match what the reader needs.

| Data structure | Owner process | Reader need | Aligned? |
|---|---|---|---|
| `created:` frontmatter | Note-creation process, immutable thereafter | "How old is this note?" for archival scoring | ✅ Perfect — created once, measures note age exactly |
| `memory/events.jsonl` | Thinking, append-only, never rewritten | Event queries ("when did X last happen") | ✅ Perfect — written at event time, never transformed |
| `inner/thoughts/<date>/` | Thinking wake process, pruned at 7 days | Wake audit, short-lived scaffolding | ✅ Perfect — created and pruned by the same process |
| `inner/surface/<date>/` | Thinking, written when insight is sharp | Speaking reads at startup for actionable items | ✅ Perfect — written by the process with the insight |
| `inner/notes/` | Speaking, consumed by Thinking | "What did Speaking observe?" for vault promotion | ✅ Perfect — written by Speaking, owned exclusively until consumed |

The contrasting cases that required fixes:

| Data structure | Intended owner | Incidental writer | Result |
|---|---|---|---|
| `last_accessed:` for archival | Speaking (real conversation access) | Thinking (grooming side effect) | Contamination — archival decisions biased toward groomed notes |
| §1 Active threads | Deferred-action tracker | Compaction LLM (rewriter) | Semantic drift — precision collapses over N cycles |
| `input_tokens` in `should_compact()` | SDK usage tracker | n/a (wrong field, not wrong writer) | Scope error — correct writer, wrong channel specified |

---

## The constructive design checklist

When introducing any new field used for scoring, ranking, or decision-making:

1. **Ownership question:** Who should be the authoritative writer for this field? Name the process, not just the field type.

2. **Incidental-write audit:** Which *other* processes have write access to this field as a side effect? If any, does that contaminate the signal for the reader's purpose?

3. **Semantic intent check:** Does the writer's output semantics match the reader's need? (A summarizer writes terse gists; if the reader needs precision, it's the wrong owner.)

4. **Immutability preference:** If the field measures a fact that doesn't change (creation time, event time, note identity), make it immutable. Immutable fields have no contamination or drift risk.

5. **Prune-path co-design:** If the structure is append-class (events, notes, wakes), budget the prune design in the same iteration. Append without prune is an ownership gap — no process owns the "stop growing" operation.

---

## Where the principle comes from

This pattern appears in well-understood engineering disciplines:

- **Database audit tables** — append-only, never written by the audited process. Clean channel ownership.
- **Double-blind trials** — the observer (scorer) never writes to the record being scored. The writer (participant) never sees the scoring criteria. Contamination-free by construction.
- **Event sourcing** — immutable append-only event log is the canonical record; derived state is computed separately and can be recomputed from scratch. The event log has a single clear owner (the event-recording process).

Alice's memory design has independently arrived at the same patterns: `events.jsonl` is Alice's audit log (append-only, single owner), `created:` is Alice's immutable baseline, the hemisphere separation (Speaking can't write memory, Thinking can't hit external APIs) enforces ownership at the process level.

The insight isn't new — it's a version of write-once / append-only / single-writer discipline that experienced systems designers know intuitively. The value here is naming it explicitly so it can be applied as a deliberate check, not just intuited case-by-case.

---

## Implication for future metrics

Before shipping any new scoring or ranking feature:

- If it reads a mutable field, ask if any process other than the intended owner has write access.
- If it reads a derived field (summaries, computed scores), trace the derivation chain — semantic drift accumulates at each transformation step.
- If it reads `last_accessed` for concept notes vs. dailies, remember: concept-note access by Speaking is meaningful; grooming access is noise. Use `speaking_accessed_at` once deployed.

---

## Related

- [[2026-04-26-metric-failure-taxonomy]] — three failure modes; this note explains their common root
- [[2026-04-26-write-read-coupling-failure]] — deep treatment of Mode 2 (contamination)
- [[2026-04-26-invisible-accumulation-pattern]] — prune-path co-design; append without prune as ownership gap
- [[2026-04-26-retrieval-protocol-extensions]] — `speaking_accessed_at` field as the correct channel for retrieval scoring
- [[design-ops-archive]] — archival policy; `created:` as the owned write-channel for daily archival
- [[2026-04-25-forgetting-mechanism-design]] — where the `last_accessed` contamination was first identified
- [[2026-04-26-irreversibility-constraint-principle]] — names the deeper reason immutability matters: irreversible losses don't share a recovery timescale with incremental gains, so constraint-first beats EV-optimization
