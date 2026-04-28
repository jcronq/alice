---
title: Design — Active-Learning Protocol (Day Mode)
aliases: [active-learning-protocol, ideas-queue, experiment-protocol]
tags: [reference, design]
created: 2026-04-25
---

# Design — Active-Learning Protocol (Day Mode)

> **tl;dr** Active mode (07:00–23:00) operational protocol: priority tiers (active-problems → interests → wandering) drained from `inner/ideas.md` (Thinking-owned); experiments run in 5-min wakes (cadence overridden 30 → 5 on 2026-04-25), produce artifacts in `cortex-memory/research/`, auto-close at `budget_wakes` cap.

Part of the [[design-day-night-modes]] split. This note covers *how* thinking operates during active-learning wakes; the parent covers the mode-split design, cadence, and implementation requirements.

---

## Priority Tiers

[[owner]]'s explicit hierarchy (2026-04-25 14:05 + 14:06 EDT):

1. **Active problems** — when Owner has something in flight, serve it. Highest priority.
2. **Topics of interest** — strong preference when no active problem. Not exclusive to Owner-topics.
3. **Free wandering** — fallback tier; runs when tiers 1 and 2 are both empty. Wandering on any topic is allowed — "not limited" means no topic restriction, not "permitted regardless of tier state." Bootstrap language: "explicitly permitted when the queue's empty."

---

## Experiment Queue: `inner/ideas.md`

Thinking self-populates. Speaking inserts Owner's requests via `inner/notes/`; thinking processes and adds to the right tier. Owner does not edit `inner/ideas.md` directly.

**Format per entry:** `[date] [source: self-seeded | from speaking note YYYY-MM-DD] <problem/topic> → <hypothesis if known>. Budget: N wakes.`

```markdown
# Ideas Queue

## Active problems (Owner-priority)
- [YYYY-MM-DD] [from speaking note] <hypothesis statement>. Budget: N wakes.

## Topics of interest
- [YYYY-MM-DD] [self-seeded] <topic to investigate>.

## Free wandering
- [2026-04-25] [self-seeded] Trace LLM memory architecture patterns.

## Paused
- [date] [PAUSED wake N/budget] <slug>. Resume: <where I left off>.
```

**Completion convention:** Completed entries are struck through **in place** (within their original tier section) with a `→ **DONE**` trailer. There is no separate `## Done` section — keeping items in-tier preserves context about why they were chosen and makes the history readable without a sink-pile. The `## Paused` section is used when an active problem interrupts a running experiment; entries resume from there.

Thinking drains top-down within tier.

If `## Active problems` is empty → tier 2. If queue is empty and no active problem → run a gap-mapping scan to generate 3–5 new seeds before proceeding.

---

## Active-Problems Mechanism

**Ownership: Thinking Alice exclusively manages `inner/ideas.md`.** Neither Owner nor Speaking edits it directly. Owner talks to Speaking via Signal; Speaking drops a note to thinking via `inner/notes/`; thinking processes the note and adds the entry to the right tier.

---

## Topics of Interest

Inferred (not explicitly maintained) from:
- Primary: anything in `cortex-memory/index.md` under Projects + Reference — these are the live, tracked topics.
- Secondary: items appearing 3+ times in recent dailies (last 14 days) that don't yet have vault coverage.
- Tertiary: concepts mentioned in `inner/notes/` but not yet in a reference note.

When choosing a tier-2 experiment, pick a topic from this set that has the most potential to produce a useful new note or expand a thin existing one.

---

## Free Wandering

Fallback tier — runs when both active-problems and topics-of-interest queues are empty. Explicitly permitted at that point. Rules:
- Must produce a visible artifact in `cortex-memory/research/` — no "thinking without output."
- Examples: read a Karpathy blog post, integrate concepts into vault; trace an LLM paper and produce a summary note; design a benchmark variant; explore adjacent CS topics (context compaction strategies, agent memory architectures).
- Preference for topics adjacent to Owner's known interests (AI, home automation, fitness, engineering) but not limited.

---

## Experiment Lifecycle

Respects the constitutional boundary: thinking reads + designs + synthesizes; speaking dispatches workers to build/execute.

```
1. Choose experiment (from active-problems / ideas queue / gap-scan)
2. Write hypothesis + budget at top of wake thought file
3. Investigate: read files, web fetch, trace code, analyze data
4. Synthesize findings into cortex-memory/research/<date>-<slug>.md
5. If actionable (bug found, code fix needed, Owner decision required) → inner/surface/
6. Drop new experiment seeds into inner/ideas.md
7. Close wake with findings summary
```

Each research note has:
```yaml
---
title: ...
status: in-progress | paused | complete
tier: active-problem | topic-of-interest | wander
hypothesis: ...
budget_wakes: 3       # default; extend with justification
wakes_spent: 1
started: 2026-04-25
updated: 2026-04-25
---
```

Budget rules:
- Default `budget_wakes: 3`. Hard cap 10 without explicit Owner approval.
- Auto-close at budget: write findings summary, mark complete (even if hypothesis unresolved — partial findings are still findings).
- Extension: thinking writes a one-line justification in the research note and extends. No approval needed under 10.

### Interruption Handling

- **Emergency (`inner/emergency/`) or high-priority note** → drain first, experiment waits.
- **Routine note from Speaking** → finish current wake's experiment chunk, drain at start of next wake.
- **Active problem added mid-experiment** → close current wake cleanly: write up findings so far, mark research note `status: paused`, add to `inner/ideas.md ## Paused`. Pivot to active problem next wake.

---

## Output: `cortex-memory/research/`

Active investigation notes. File naming: `YYYY-MM-DD-<slug>.md`.

```
cortex-memory/research/
  YYYY-MM-DD-<topic-slug>.md
  YYYY-MM-DD-<topic-slug>.md
  YYYY-MM-DD-<topic-slug>.md
```

These are active investigation notes, not stable reference. Once an investigation concludes and produces durable knowledge, the durable parts migrate to the appropriate `reference/` or `projects/` note; the research note stays as-is for provenance.

---

## Wake Logging Changes

Wake file frontmatter additions (vs REM mode):

```yaml
mode: active-learning
tier: active-problem | topic-of-interest | wander
experiment: <slug>  # null when just draining inbox
```

---

## Corpus health monitoring

After high-volume production days (>30 new research notes), the synthesis corpus compresses: Stage D pair diversity narrows, later notes self-reference earlier same-day notes (closed loop), and base tags saturate. The cost is opportunity-cost rather than damage: too many notes from one day compresses cross-domain distance for Stage D.

**Detection:** Compute `corpus_diversity_score` = distinct creation dates in last-7-days research corpus / total research notes in last 7 days. Score approaching 1.0 means one-day dominance; ideal ≤ 0.4.

**Adaptive behavior:**
- When diversity score ≥ 0.5: prefer **consolidation + promotion** over new research in the current wake — read existing research, extract durable findings to `reference/` or `projects/` notes, look for merge-worthy overlaps.
- After >30 new research notes in a single day: the following 1–2 active wakes default to consolidation mode before resuming generative research.
- Include the corpus diversity score as a contextual factor in the Stage D quality morning sample — high diversity score explains elevated forced-connection rate even with a well-tuned pair algorithm.

Full design: [[2026-04-26-research-production-mrv]]

---

## Related

- [[design-day-night-modes]] — the parent design: mode split, cadence, REM protocol, implementation requirements
- [[memory-layout]] — where research notes live vs reference vs dailies
- [[design-ops-archive]] — archival policy for the research/ notes this protocol generates (60-day criterion; semi-automatic; surface-gated concept retirement)
- [[alice-speaking]] — speaking hemisphere; owns the daemon, dispatches workers based on surfaces
- [[design-thinking-capabilities]] — unified operational envelope: synthesizes this note + day-night-modes + ops-archive into a single reference
- [[2026-04-26-research-production-mrv]] — source: MRV analog + corpus diversity analysis that motivated the §Corpus health monitoring section
