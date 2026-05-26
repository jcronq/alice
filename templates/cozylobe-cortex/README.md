---
title: cozylobe-cortex
tags: [cozylobe-cortex, vault]
created: 2026-05-26
updated: 2026-05-26
---

# cozylobe-cortex

Home-specific knowledge graph the cozylobe motion-cortex pipeline reads
and writes. Lives at `~/alice-mind/cozylobe-cortex/` on a deployed Alice
host. The templates in this directory are copied into place by the
onboarding CLI (`scripts/cozylobe_cortex_onboard.py`) on first run.

## Layout

| Subdir          | Purpose                                                            |
| --------------- | ------------------------------------------------------------------ |
| `rooms/`        | One note per room (name, adjacency, sensors that cover it)         |
| `sensors/`      | One note per motion sensor (entity_id, room, kind, install date)   |
| `people/`       | Jason, Katie, Mike, unknown — time-of-day patterns                 |
| `destinations/` | Semantic locations (kitchen at 07:00 = breakfast)                  |
| `trajectories/` | Observed room sequences with weights (initially empty)             |
| `guesses/`      | Current and recent positional inferences (initially empty)         |

Each subdir's `README.md` documents the note schema. Edit those rather
than this top-level file if you're adding fields.

## Edge syntax

cozylobe-cortex adopts the canonical typed-weighted-edges syntax from
day one. Relationships are written inline in note prose:

```
The Kitchen (IS-ADJACENT-TO:1.0)[[rooms/Living Room]] and
(IS-ADJACENT-TO:0.8)[[rooms/Dining Room]] (the dining wall is half-
open so motion bleeds between them).
```

* Bare `[[target]]` → `SEE-ALSO` with weight `1.0`.
* `(VERB)[[target]]` → verb specified, weight defaults to `1.0`.
* `(VERB:0.7)[[target]]` → both specified.

The controlled vocabulary lives in `src/alice_cozylobe/cortex.py` as
`CONTROLLED_VERBS`. cozylobe-specific predicates: `IS-ADJACENT-TO`,
`COVERS`, `OFTEN-VISITS`, `MOTION-TRIGGERED`, `CONFIRMED-BY`. The 11
domain-general predicates from the vault-tiering design also apply.

## Lint

`python scripts/cozylobe_cortex_lint.py [--vault PATH]` walks the vault
and checks:

* Every sensor's `room:` resolves to an existing room.
* Every room's `adjacent:` entries resolve to existing rooms.
* No orphan notes (notes outside the six categorical subdirs).
* Inline edge syntax parses cleanly (warns on unknown verbs).

Used by the onboarding script after writing initial notes, by the test
suite, and by thinking when grooming the vault.

## Phase 1 scope

This README, the per-subdir READMEs, the onboarding CLI, the lint
command, and the `alice_cozylobe.cortex` read library are Phase 1 of
issue #378. Motion classification (Phase 2), the guess lifecycle
(Phase 3), and the qwen reasoning loop (Phase 4) land in subsequent
issues — none of them write to the vault from this codebase yet.

Design: `cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`.
