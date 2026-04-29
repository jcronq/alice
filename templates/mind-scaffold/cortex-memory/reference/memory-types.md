---
title: Memory types — cortex-memory taxonomy
aliases: [memory types, node types, vault taxonomy, memory categories]
tags: [reference]
created: 2026-04-29
---

# Memory types — cortex-memory taxonomy

> **tl;dr** Each cortex-memory folder has a distinct purpose and graph color. Dated `research/` notes are investigation artifacts, not daily logs.

## Type map

| Folder | Type | Purpose | Graph color |
|---|---|---|---|
| `people/` | **Entity** | Atomic notes about specific people (Owner, friends, family) | amber `#f9c96d` |
| `projects/` | **Entity** | System / project notes (one per recurring project) | light blue `#8fc7ff` |
| `reference/` | **Reference** | How-things-work docs, design records, policies | blue-gray `#b8d4ff` |
| `feedback/` | **Feedback** | Owner's preferences, behavioral rules, lessons learned | salmon `#ffa07a` |
| `sources/` | **Source** | External materials, articles, papers referenced in research | teal `#7de3c4` |
| `conflicts/` | **Conflict** | Contradictions between vault notes awaiting resolution | red `#ff6b81` |
| `dailies/` | **Daily** | Chronological activity logs (one file per calendar day) | light gray `#d4d4d4` |
| `research/` | **Research** | Investigation artifacts from active-learning sessions | light green `#a8e6cf` |

## Key distinction: "dated" vs "daily"

`research/` notes have dates in their filenames (e.g. `2026-04-25-write-path-design.md`) but are **not** daily logs. They're investigation artifacts from specific thinking sessions — same date-stamped naming convention, different purpose.

- **Daily** = "what happened today" — one per day, appended throughout the day, links out to everything touched
- **Research** = "what I figured out during an investigation" — born once, rarely updated, read-only after creation

Daily logs live in `dailies/`. Research artifacts live in `research/`. Both get dates in their names. This is intentional.

## Folder-rename considerations

`people/` vs `entities/`: if non-person entities (organizations, services) get their own notes, a rename to `entities/` could make sense. Until then `people/` is accurate.
