---
title: Memory layout policy
aliases: [memory layout, where things live]
tags: [reference]
created: 2026-04-29
---

# Memory layout policy

> **tl;dr** `cortex-memory/` is the source of truth for facts. `memory/` holds the event stream and runtime data. `inner/` is ephemeral working memory.

## cortex-memory/ (source of truth for facts)

Groomed, wikilinked, Obsidian-compatible vault. Single source of truth for any durable fact about [[owner]], people the owner talks to, Alice's systems, and behavioral rules.

Navigate via [[index]]; folder taxonomy at [[memory-types]].

| Folder | Purpose |
|--------|---------|
| `people/` | Per-person notes (Owner and trusted contacts) |
| `projects/` | Active project hubs |
| `reference/` | Durable reference facts, design docs, policy |
| `feedback/` | Behavioral rules extracted from owner feedback |
| `sources/` | External source summaries |
| `dailies/` | Chronological activity log, one file per day |
| `research/` | Investigation artifacts and synthesis notes |
| `conflicts/` | Active contradictions between vault notes |

## memory/ (event stream + runtime data)

Home for `events.jsonl` — append-only structured event log (meals, workouts, weights, errors, reminders, etc.) — and any operational subfolders that skills code references by literal path. Domain-specific; populate as you wire skills.

## inner/ (working memory — ephemeral)

Thinking Alice's operational scratch space. Not a source of truth; content here is consumed and promoted elsewhere.

| Path | Purpose | Retention |
|------|---------|-----------|
| `inner/notes/` | Inbox: fleeting notes from Speaking → consumed by Thinking | Move to `.consumed/` after processing |
| `inner/surface/` | Outbox: Thinking → Speaking; actionable findings + proposals | Keep until Speaking moves to `.handled/` |
| `inner/thoughts/<YYYY-MM-DD>/` | Wake files: per-wake intent + close summary | 7-day rolling delete; the daily log is the durable record |
| `inner/ideas.md` | Experiment backlog; Thinking-owned queue | Live document; entries marked DONE in place |
| `inner/state/` | Session state (e.g. `session.json`, `speaking-turns.jsonl`) | Operational; never archive |

## Conflict resolution

When `cortex-memory/` and any other location disagree, **cortex wins**. If cortex doesn't have a fact yet, the other location is authoritative until promoted.
