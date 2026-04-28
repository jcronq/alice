---
title: Memory types — cortex-memory taxonomy
aliases: [memory types, node types, vault taxonomy, memory categories]
tags: [reference]
created: 2026-04-25
---

# Memory types — cortex-memory taxonomy

> **tl;dr** Eight cortex-memory folder types, each with a distinct purpose and graph color; dated `research/` notes are investigation artifacts, not daily logs.

## Type map

| Folder | Type | Purpose | Count | Graph color |
|---|---|---|---|---|
| `people/` | **Entity** | Atomic notes about specific people (Owner, Friend, Alice) | 2 | amber `#f9c96d` |
| `projects/` | **Entity** | System / project notes (one per recurring project, e.g. alice-speaking, alice-viewer) | 6 | light blue `#8fc7ff` |
| `reference/` | **Reference** | How-things-work docs, design records, policies | 38 | blue-gray `#b8d4ff` |
| `feedback/` | **Feedback** | Owner's preferences, behavioral rules, lessons learned | 14 | salmon `#ffa07a` |
| `sources/` | **Source** | External materials, articles, papers referenced in research | 4 | teal `#7de3c4` |
| `conflicts/` | **Conflict** | Contradictions between vault notes awaiting resolution | 0 | red `#ff6b81` |
| `dailies/` | **Daily** | Chronological activity logs (one file per calendar day) | 44 | light gray `#d4d4d4` |
| `research/` | **Research** | Investigation artifacts from active-learning sessions | 100 | light green `#a8e6cf` |

**Total: 211 notes** (as of 2026-04-26 21:57 EDT)

## Key distinction: "dated" vs "daily"

`research/` notes have dates in their filenames (e.g. `2026-04-25-write-path-design.md`) but are **not** daily logs. They're investigation artifacts from specific thinking sessions — same date-stamped naming convention, different purpose.

- **Daily** = "what happened today" — one per day, appended throughout the day, links out to everything touched
- **Research** = "what I figured out during an investigation" — born once, rarely updated, read-only after creation

Daily logs live in `dailies/`. Research artifacts live in `research/`. Both get dates in their names. This is intentional.

## Colormap implementation status

**Shipped 2026-04-26** — commit `1e760a8` deployed to alice-viewer. The eight cortex-memory folder colors in the table above are live in the graph. Type-filter pill bar also added (filter by folder type). The old "everything is purple" bug is fixed.

## Folder-rename considerations

`people/` vs `entities/`: current content is all people, so `people/` is accurate. If non-person entities (organizations, services) get their own notes, a rename to `entities/` could make sense. Not pressing — 0 misclassified notes today.

## Related

- [[memory-layout]] — where things live across cortex-memory, memory/, and legacy paths
- [[alice-viewer]] — the graph UI that uses these types for coloring
- [[index]] — vault entry point with link inventory
