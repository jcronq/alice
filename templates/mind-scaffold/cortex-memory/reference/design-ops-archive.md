---
title: Design — ops/archive
aliases: [ops-archive, archive-op, vault-archival, archive skill]
tags: [reference, design]
created: 2026-04-25
---

# ops/archive — retire stale notes to cold tier

> **tl;dr** Spec for the `cortex-memory/ops/archive` skill: rolling 90-day archival of dailies, semi-automatic research archival, and surface-gated concept-note retirement.

Design basis: [[2026-04-25-forgetting-mechanism-design]]. Ready to deploy as `.claude/skills/cortex-memory/ops/archive.md` — see Related.

## Upcoming archival wave

As of 2026-04-26: **archive trigger has already been met.** One daily is past the 90-day threshold now; 20 more cross it through May 2026.

**Already past threshold (require archival now):**

| Daily | Age | Notes |
|---|---|---|
| 2025-06-12 | 318 days | Session zero — founding context for [[owner]], fitness scaffolding. High-value; archive to `archive/dailies/2025/`. Wikilinks to [[fitness]], [[signal-cli]] should remain accessible. |

**Crossing threshold through May 2026 (first wave):**

| Daily | Threshold date |
|---|---|
| 2026-02-01 | 2026-05-02 |
| 2026-02-02 | 2026-05-03 |
| 2026-02-03 | 2026-05-04 |
| 2026-02-06 | 2026-05-07 |
| 2026-02-08 | 2026-05-09 |
| 2026-02-09 | 2026-05-10 |
| … | … |
| 2026-02-25 | 2026-05-26 |

All 20 February 2026 dailies are eligible by May 26. Current daily count: 44 (threshold trigger: > 60 — not yet met on count alone, but the age trigger is). **Blocker:** `ops/archive.md` must be deployed by Speaking before any archival can run. Surface `2026-04-25-173400-ops-archive-deployment.md` filed; awaiting action.

## Trigger

- Daily count in `cortex-memory/dailies/` exceeds 60, OR
- Any daily's `created` date is > 90 days ago, OR
- Any `research/` note's `created` date is > 60 days ago AND topic is resolved, OR
- Thinking consciously decides to reduce graph noise

## Archive layout

```
cortex-memory/
  archive/
    dailies/
      2025/
      2026/
    research/
    sources/
    reference/
```

Archived notes remain in the Obsidian vault with full wikilinks — they just leave the main graph, reducing visual noise.

## Category-gated rules

| Category | Archive? | Criterion |
|---|---|---|
| `people/` | **NEVER** | Identity-critical |
| `feedback/` | **NEVER** | Active behavioral constraints |
| `projects/` (active) | No | Only after marked done/abandoned |
| `projects/` (done) | Candidate | 90+ days after status → done/abandoned |
| `dailies/` | Yes, rolling | `created` > 90 days ago → archive by year |
| `research/` | Candidate | `created` > 60 days AND topic resolved/superseded |
| `reference/design-*` | Candidate | Age > 180 days AND access_count < 3 AND superseded |
| `reference/` (other) | Candidate | Age > 180 days AND access_count < 2 AND links ≤ 1 |
| `sources/` | Candidate | Age > 180 days AND access_count < 3 AND links ≤ 1 |
| `conflicts/` | Candidate | `resolved:` frontmatter set AND > 30 days old |

**Exception**: notes tagged `policy` are never auto-archived regardless of age.

## Step 1 — daily archival (automatic)

1. List all `cortex-memory/dailies/*.md`; filter to `created:` > 90 days before today.
2. For each candidate:
   - Add `archived: YYYY-MM-DD` to frontmatter.
   - Move to `cortex-memory/archive/dailies/YYYY/YYYY-MM-DD.md`.
3. Update `index.md`: replace individual daily links with a range note.
4. Log count and date range archived to today's daily.

## Step 2 — research archival (semi-automatic)

1. List `cortex-memory/research/*.md` where `created:` > 60 days ago.
2. For each candidate:
   - Check if the subject note reflects the findings.
   - If yes → add `archived:` to frontmatter, move to `archive/research/`.
   - If no → surface a gap note to `inner/surface/`, then archive with a processing trailer.
3. Log count archived.

## Step 3 — concept note candidates (surface, never auto-archive)

1. For `reference/`, `sources/`, done `projects/`: compute notes where age > 180 days AND `access_count < 3` AND incoming links ≤ 1.
2. Produce a markdown table and drop into `inner/surface/YYYY-MM-DD-HHMMSS-archive-candidates.md` with `reply_expected: true`.
3. Do **not** move concept notes without explicit approval.

## Rules

- Never archive `people/`, `feedback/`, or `policy`-tagged notes.
- Never auto-archive concept notes — always surface first.
- Never delete — archive is a move, not a purge.
- Don't run during an active problem.

## Budget

1–2 wakes: daily archival in wake 1, research + concept-surface in wake 2.

## Related

- [[2026-04-25-forgetting-mechanism-design]] — theoretical basis
- [[memory-layout]] — vault structure this op modifies
- [[design-active-learning-protocol]] — generates the research/ notes that will accumulate
- [[design-thinking-capabilities]] — unified operational envelope: synthesizes this note + day-night-modes + active-learning-protocol into a single reference
- [[2026-04-27-stage-d-corpus-archival-policy]] — Stage D-specific archival policy extending the `research/` criteria; quality-tier-gated retention windows
