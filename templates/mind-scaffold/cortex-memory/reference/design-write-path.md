---
title: Alice write-path conventions
aliases: [write path, note write path, speaking note conventions, drain conventions]
tags: [reference, design]
created: 2026-04-25
---

# Alice write-path conventions

> **tl;dr** File a note when the session produced a new durable fact, event, or correction. Skip ephemeral Q&A, iterative drafts, and task-coordination chatter. Four optional note fields let Thinking drain the inbox with less inference overhead.

## Context

[[alice-speaking]] routes durable facts to Thinking via `inner/notes/`. Thinking drains on every wake, inferring type and routing from prose. Three years of organic use produced a write path that works — but specific, small conventions would reduce inference overhead without requiring format changes.

Settled from investigation in `research/2026-04-25-write-path-design.md`.

## When to file a note

The gating question: **would Thinking need this in a future session?** If yes, file. If no, skip.

### File when the session produced:

| Category | Examples | Notes |
|---|---|---|
| **New persistent fact** | Owner's preference, a config value, a system state change | File even if the fact seems small |
| **Trackable event** | Meal, workout, weight, deployment, service restart, config change, error | These go to both daily + events.jsonl |
| **Durable artifact** | A design doc, a final approved description, a specification | Only the *approved* version, not drafts |
| **Correction** | Speaking answered incorrectly; vault note is wrong or stale | File so Thinking can update the source of truth |
| **Explicit preference** | "I always want X," "never do Y," "from now on..." | Durable behavioral rules |

### Skip when:

| Category | Examples | Why |
|---|---|---|
| **Ephemeral Q&A** | "What is s6?" answered from existing knowledge | No new fact; vault already has it or it's general knowledge |
| **Iterative draft output** | Owner asked for a paragraph, then "redo it" twice | Only file the *accepted final* if Owner explicitly wants it saved |
| **Task coordination** | "Start this," "check that," mid-task clarifications | Pure orchestration; produces no durable facts |
| **Transient context** | Owner gave context just for this task, no lasting implication | If it won't matter next week, skip it |

### Edge cases:

- **Q&A that reveals a vault gap** — if Speaking had to invent an answer because the vault was missing the fact, file a note so Thinking can verify and add it.
- **Worker completion reports** — file when the completion marks a meaningful state change (new code deployed, significant rebuild). Skip routine "task done" handshakes.
- **"Save this"** — if Owner explicitly says keep a draft/output, file it as a `type: artifact` note.

## Current format (baseline)

Speaking's de facto format:

```markdown
# note — 2026-04-25T10:52:03-04:00
tag: ha-thinning-strategic-direction

prose here. numbered observations. "no reply needed" or "surface when ready."
```

This works. Everything below is additive — improvements when used, no breakage when omitted.

## Optional enhancements

### A. `type:` field (Speaking-side)

```markdown
type: factual-update | event | design | correction | surface-trigger
```

When present, Thinking skips full-read inference and routes immediately. When absent, current behavior (infer from prose) continues.

**Wins:** eliminates the read-before-route overhead. Useful when Speaking knows the type is unambiguous (e.g., a meal log is always `type: event`).

### B. `related:` field (Speaking-side)

```markdown
related: [[project-a]], [[project-b]]
```

When present, Thinking starts by checking those notes — and their Related sections — for cross-cuts before searching. Eliminates the search-and-match step on drain.

**Wins:** Speaking already knows which vault note needs updating; naming it saves Thinking a mini-search.

### C. Backward-linking pass (Thinking-side, drain step)

After processing a note, scan the vault for notes that mention the note's key entities (project, person, concept). For each hit: is the related note stale? Does it need a cross-reference to the new information?

**Wins:** catches cross-cuts in the same wake rather than a subsequent one. Example: a bug fix that naturally touches multiple related notes — a backward-link pass on drain finds all of them at once.

**Cost:** one search per drained note. Worth it when the note mentions more than one key entity.

### D. `dedup_key:` field (Speaking-side, events only)

```markdown
dedup_key: meal-2026-04-25-12
```

For event notes (meal, workout, weight): Speaking includes this field. Thinking checks `memory/events.jsonl` for a matching `dedup_key` before appending. Prevents duplicate events from connectivity blips or double-taps.

## What to keep as-is

- **Freeform prose** — structured schemas create friction inside a live conversation. Keep it.
- **`became:` as Thinking's artifact** — only Thinking knows which vault notes will be touched after the drain. Speaking cannot and should not fill this.
- **`tag:` as freeform slug** — readable, scannable, sufficient. No enum required.

## Related

- [[alice-speaking]] — the hemisphere writing the notes; improvements here are for her
- [[memory-layout]] — current structure of Alice's memory tiers
- [[design-unified-context-compaction]] — the unified intake flow this hooks into
- [[llm-agent-memory-survey-2025]] — survey that flagged write path as chronically underengineered
