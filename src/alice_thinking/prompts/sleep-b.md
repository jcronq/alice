## Step 0 — sleep mode, Stage B (Consolidation)

Sleep mode (23:00–06:59 local), Stage B. The harness selected this phase deterministically — inbox has items OR vault has issues (broken wikilinks, orphan stubs, frontmatter drift), OR the cascade fell through to B as the safe default. Stage B is the workhorse: inbox drain, link audit, frontmatter normalize, orphan linking. Real work always wins. Full design: [[2026-04-25-sleep-architecture-design]]; adaptive escalation: [[2026-04-26-adaptive-stage-selection-design]].

## Step 3 — do the work (Stage B — Consolidation)

After draining notes (Step 2), run the existing groom-the-vault behavior. Invoke the **cortex-memory** skill at `.claude/skills/cortex-memory/SKILL.md` and pick **one** op:

- Open dangling links in `cortex-memory/unresolved.md` → `ops/document`
- Concept or research notes larger than 250 lines, or tl;drs with "and" → `ops/atomize` (never atomize dailies)
- Orphan notes (zero incoming wikilinks) → `ops/link`
- Broken wikilinks, stale tl;drs, drifted frontmatter → `ops/groom`
- Recurring theme across recent dailies / consumed notes → `ops/promote`
- External source Jason asked about → `ops/reference`
- Two notes disagree → `ops/conflict`

**One small pass per wake.** Finish cleanly.

**After main op, if budget allows:** lint one stale finding — pick one `status: open` or `status: proposal` research note with `updated:` >7 days old; check whether its problem is now resolved; call `ops/resolve` if so. One note per Stage B wake, side-check only. Full spec: [[2026-04-28-cortex-signal-architecture]] §5.

**Also if budget allows:** shadow-neighbor access — if the main op's target note has ≥5 outgoing links and at least one neighbor has `access_count: 0`, read one dormant neighbor (pick randomly from the access_count=0 neighbors), bump its `access_count`, add a one-line tl;dr if missing. One neighbor per wake. Rationale: hub inhibition shadow — top hubs have 18–64% dormant neighbors that are unreachable under normal grooming; without this step, the ripped-by-40 and fitness research corpus stays in permanent shadow. See [[2026-04-28-hub-inhibition-shadow-audit]].

**Also if budget allows (after stale-finding lint and shadow-neighbor):** conflict scan — take the main op's target note, collect its outgoing wikilinks (skip dailies), sort by `updated:` descending, read the top 2. If either makes a factual claim that directly contradicts the groomed note — same quantity with different confirmed values, or same event with a different date and the later note is actually newer per `updated:` — create a `conflicts/<today>-<slug>.md` entry via `ops/conflict`. Do not flag superseded notes, proposals vs. confirmed facts, or different framings. One conflict per wake, then stop. Silent null result is correct — no logging needed when nothing is found. Budget: +2 reads + 1 optional conflict write. See [[2026-05-05-stage-b-active-conflict-detection]].

Conflicts: if you can't resolve a contradiction alone, follow `ops/conflict` — log under `cortex-memory/conflicts/`, try self-resolution first, surface to Speaking via `inner/surface/` only when stakes matter and resolution isn't obvious. Budget: at most one surface per wake.

Begin.
