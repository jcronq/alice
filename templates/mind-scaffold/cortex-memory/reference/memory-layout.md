---
title: Memory layout policy
aliases: [memory layout, where things live, cortex vs legacy]
tags: [reference]
created: 2026-04-24
---

# Memory layout policy

> **tl;dr** cortex-memory/ is the sole source of truth; legacy memory/ holds the event stream, runtime data, and pre-2026-04-24 dailies — cortex wins on conflict.

## Canonical storage

### cortex-memory/ (source of truth for facts)

Groomed, wikilinked, Obsidian-compatible vault. Single source of truth for:
- People, projects, reference, feedback, and sources notes.
- Any durable fact about [[owner]], [[friend]], Alice's systems, and behavioral rules.

Navigate via `cortex-memory/index.md`.

**Folder inventory (all under cortex-memory/):**

| Folder | Purpose | Schema |
|--------|---------|--------|
| `people/` | Per-person notes (Owner, Friend, etc.) | frontmatter: aliases, tags [people] |
| `projects/` | Active project hubs | frontmatter: tags [project] |
| `reference/` | Durable reference facts, design docs, policy | frontmatter: tags [reference] |
| `feedback/` | Behavioral rules extracted from Owner feedback | frontmatter: tags [feedback] |
| `sources/` | External source summaries (papers, articles, third-party data) | frontmatter: tags [source] |
| `dailies/` | Chronological activity log, one file per day | frontmatter: tags [daily]; never atomized |
| `research/` | Investigation artifacts and synthesis notes | frontmatter: status, tags [research]; migrate durable findings to reference/ |
| `conflicts/` | Active contradictions between vault notes | frontmatter: tags [conflict]; resolve or surface |
| `decisions/` | Architectural decision records (ADRs) — added 2026-04-28 | full index: [[decisions-index]]; ADR schema in [[2026-04-28-cortex-signal-architecture]] §3 |
| `findings/` | Hypothesis test results — added 2026-04-28 | full index: [[findings-index]]; finding schema in [[2026-04-28-cortex-signal-architecture]] §5 |

### memory/ (event stream + runtime data)

Home for:
- `events.jsonl` — append-only structured event log (meals, workouts, weights, errors, reminders, etc.).
- Operational subfolders for runtime data that the skills code references by literal path. Domain-specific — populate as you wire skills.

### inner/ (working memory — ephemeral)

Thinking Alice's operational scratch space. Not a source of truth; content here is consumed and promoted elsewhere.

| Path | Purpose | Retention |
|------|---------|-----------|
| `inner/notes/` | Inbox: fleeting notes from Speaking → consumed by Thinking | Delete after processing (moved to `.consumed/`) |
| `inner/surface/` | Outbox: Thinking → Speaking; actionable findings + proposals | Keep until Speaking moves to `.handled/` |
| `inner/thoughts/<YYYY-MM-DD>/` | Wake files: per-wake intent + close summary | **7-day rolling delete** (see [[2026-04-26-thoughts-pruning-policy]]); daily log is the durable record |
| `inner/ideas.md` | Experiment backlog; Thinking-owned queue | Live document; entries marked DONE in place |
| `inner/state/` | Session state: `session.json`, `speaking-turns.jsonl` | Operational; never archive |

**`inner/thoughts/` growth rate:** ~250 files/day at 5-min REM cadence, ~85+ files for an active-mode morning. Design: delete directories older than 7 days at Step 5 close. No compression needed — `cortex-memory/dailies/<date>.md` already records what each wake did.

### Conflict resolution

`CLAUDE.md` rule: **cortex wins** when cortex and legacy disagree. If cortex doesn't have a fact yet, legacy is authoritative until promoted.

## Related

- [[index]]
- [[owner]]
- [[memory-types]] — taxonomy of cortex-memory folder types with purposes and graph colors
- [[design-write-path]] — settled conventions for Speaking's notes (type, related, dedup_key, backward-linking pass)
- [[design-unified-context-compaction]] — context-compaction architecture across hemispheres
- [[secrets-management]] — where secrets live, naming convention, migration plan for 6 in-vault credentials
- [[2026-04-25-forgetting-mechanism-design]] — research note: category-gated archival design, `archive/` layout, `ops/archive` sketch, revised daily criterion (use `created` not `last_accessed` for archival candidacy)
- [[design-ops-archive]] — finalized `ops/archive` spec: trigger criteria, category-gated rules, three-step procedure
- [[2026-04-26-vault-access-count-analysis]] — access_count as a grooming-frequency metric: 21 zero-access non-dailies (expected pattern for recent research); count discrepancy found and fixed (112→114 total)

## Recent synthesis

*Night 2 Stage D synthesis — 2026-04-28*

- [[2026-04-28-composite-unit-quality-gradient]] — Composite-unit quality gradient: aggregate units conceal internal variance that only appears at the component level
- [[2026-04-28-corrective-substrate-mismatch]] — Corrective substrate mismatch: default recovery mechanisms reinforce failure when they target the wrong layer
- [[2026-04-28-discovery-protocol-immutable-channel-requirement]] — Discovery protocols must measure growth via immutable channels to prevent measurement drift
- [[2026-04-28-discrete-proxy-inadequacy-principle]] — Discrete proxies for continuous substrates systematically mislead: instrument the substrate, not the proxy
- [[2026-04-28-drift-warrant-as-open-loop-metric]] — Drift-warrant as open-loop metric: vault notes under active correction are correction-pending readings, not ground truth
- [[2026-04-28-embedded-calibration-epoch-silent-drift-repair-failure]] — Embedded calibration epoch: silent drift accumulates when calibration windows are fixed and the underlying process has moved
- [[2026-04-28-granular-vs-positional-value-consolidation]] — Granular value consolidates across contexts; positional value does not: vault design implication for note atomicity
- [[2026-04-28-latent-state-clean-measurement-track]] — Latent state estimation requires a clean measurement track: corrupted inputs produce systematically biased state estimates
- [[2026-04-28-phase-aware-synthesis-zeitgeber]] — Phase-aware synthesis needs a zeitgeber: the arc model drifts without an external phase signal to anchor stage transitions
- [[2026-04-28-prerequisite-axis-ordering-two-constraint-protocols]] — Prerequisite-axis ordering: two-constraint protocols silently fail when the axes are not ordered by dependency
- [[2026-04-28-proxy-variable-drift-threshold-failure]] — Proxy-variable drift and threshold failure: time as a stand-in for condition produces silent threshold inversions
- [[2026-04-28-synthesis-coverage-utility-gap]] — Synthesis coverage does not equal synthesis utility: the zeitgeber misses a dimension by optimizing coverage without retrieval routing
- [[2026-04-28-upstream-prerequisite-silent-failure-delayed-consequence]] — Upstream prerequisite failure produces silent downstream failure with delayed detection
- [[2026-04-28-write-path-omission-bias-event-sourcing]] — Write-path omission bias: the event-sourcing failure mode CQRS does not prevent
