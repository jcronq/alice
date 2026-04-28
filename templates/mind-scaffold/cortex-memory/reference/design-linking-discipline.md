---
title: design-linking-discipline
aliases: [linking-discipline, wikilink-authoring-rules]
tags: [design, cortex-memory, vault-health]
status: active
created: 2026-04-27
---

# Design: Linking Discipline

> **tl;dr** Wikilinks should express dependency, not association. Each note: 2–4 in-domain links + 1–2 deliberate cross-domain bridges. Never link because a name appears; link because the current note depends on or extends the target.

---

## The rule

Before adding `[[X]]` in a **topical note**, ask: *"Would this note be incomplete or wrong without X?"*

- **Yes** → keep the link. It's earned.
- **No** → drop it. It's courtesy or keyword noise.

**Dailies are exempt.** Daily logs (`cortex-memory/dailies/`) are chronological records that link freely across all domains — that is their function. The linking discipline applies to topical notes (`people/`, `projects/`, `reference/`, `research/`, `feedback/`, `sources/`) only.

---

## Expected lobes

The vault should form distinct domain clusters in the `/memory` graph. Each domain has a hub note; notes in that domain link primarily to siblings and the hub.

| Domain | Hub notes |
|--------|-----------|
| Active project A | `projects/<project-a>` and its design/reference notes |
| Active project B | `projects/<project-b>` and its design/reference notes |
| Alice infrastructure | `projects/alice-speaking`, design docs |
| People | `people/owner`, `people/friend` |
| Vault / memory architecture | `reference/design-thinking-capabilities`, `reference/memory-layout` |

---

## Density targets

| Note type | Max outgoing links | Cross-domain bridges |
|-----------|-------------------|---------------------|
| Atomic reference/research | 4–6 | 1–2 |
| Daily log entry | Free — links across any domain | Exempt from rule |
| Design doc | 6–10 (concepts it builds on) | 2–3 |
| Hub/index note | Uncapped (by definition) | Uncapped |

*Dailies are excluded from cluster diagnostics.* They act as artificial bridges between all domains and will inflate the full-graph hairball regardless of topical-note discipline. Modularity and lobe-coverage metrics are computed on the topical subgraph only.

---

## What counts as a cross-domain bridge

A link from domain A to domain B is a **bridge** if:
- The current note genuinely synthesizes or depends on both domains.
- The linked note is the *correct authoritative source* (a hub or research note), not just any note in domain B.

Bridges are worth adding intentionally; they're the substrate of Stage D synthesis. They're also how the lobe-coverage metric detects a cohesive cluster (a few bridges between dense lobes = healthy; many weak links between all nodes = hairball).

---

## Evaluation during Stage C hub audit

When classifying incoming links to a hub node:

| Class | Definition | Action |
|-------|-----------|--------|
| Earned | Linking note depends on or extends the hub | Keep |
| Keyword-only | Hub name appears in text; note doesn't depend on it | Drop |
| Courtesy | Linked to acknowledge importance | Drop |
| Stale | Source is archived, consumed, or superseded | Drop |

Log classifications before editing. See [[design-graph-cluster-quality]] §Stage C hub audit op.

---

## Related

- [[design-graph-cluster-quality]] — why this rule exists; hub audit procedure; modularity metrics
- [[design-thinking-capabilities]] — Stage C ops spec
- [[2026-04-25-alice-viewer-memory-graph]] — graph rendering pipeline
