---
title: "Daily creation gap — Stage C/D wake before Stage B"
aliases: [daily-creation-gap, daily-init-guard]
tags: [design, alice-thinking, sleep-architecture, fix]
created: 2026-04-26
related: [[2026-04-25-sleep-architecture-design]], [[2026-04-26-adaptive-stage-selection-design]], [[2026-04-26-sleep-stage-prediction-tonight]]
---

# Daily creation gap — Stage C/D wake before Stage B

> **tl;dr** With adaptive stage-selection live, Stage B may never run early in the night (stable vault → Stage C/D directly). Fixes: a daily-init guard in `thinking-bootstrap.md` Step 2 creates `cortex-memory/dailies/<YYYY-MM-DD>.md` on every wake if missing, before inbox drain. **Implemented 2026-04-26 ~15:00 EDT.**

---

## The problem

Stage B normally creates today's daily as a side effect of inbox draining (the first active-mode wake, or any Stage B wake that processes notes referencing a new day). With the [[2026-04-26-adaptive-stage-selection-design]] patch live, a stable vault + empty inbox causes Stage C/D to fire directly at 23:00 — Stage B may run zero times before sunrise. The stage-flip at 23:00 EDT changes the calendar date to the next day. No daily exists for it.

Effect: synthesis notes written at 00:30, 01:00, etc. have no corresponding daily entry until the morning active-mode Step 3 preamble creates the daily at ~07:10. **7-hour gap in the daily log.**

First occurrence: predicted for night of 2026-04-26 → 2026-04-27. Identified at wake 160 during gap analysis in [[2026-04-26-sleep-stage-prediction-tonight]].

---

## The fix

**Daily initialization guard in `thinking-bootstrap.md` Step 2** — runs unconditionally on every wake, independent of stage:

```
Before draining the inbox, check whether cortex-memory/dailies/<YYYY-MM-DD>.md
exists for today. If not, create it with standard frontmatter:

    ---
    title: YYYY-MM-DD
    tags: [daily]
    created: YYYY-MM-DD
    updated: YYYY-MM-DD HH:MM EDT
    last_accessed: YYYY-MM-DD
    access_count: 0
    ---

    # YYYY-MM-DD

This runs every wake regardless of stage.
```

Placement: first sub-section of Step 2, before inbox-drain logic. `updated:` uses minute precision (no seconds) to match existing dailies.

---

## Status

**Implemented 2026-04-26 ~15:00 EDT.** Speaking patched `prompts/thinking-bootstrap.md` Step 2 immediately on receipt of the surface. Uncommitted in `alice-mind`; Owner owns commit timing. Goes live for tonight's wakes (23:00 EDT). The fix means `2026-04-27.md` will exist before the first Stage C wake tonight.

Surface: `inner/surface/.handled/2026-04-26/2026-04-26-145700-daily-creation-gap.md` (handled internally, not voiced).

---

## Why not the morning preamble

The morning active-mode Step 3 preamble could create the daily retroactively, but that leaves the nightly gap. Synthesis notes written at 00:30 would have no daily entry until 07:10. The guard at Step 2 fixes the root cause — at most one wake (the very first of the day) ever sees a missing daily.

---

## Related

- [[2026-04-25-sleep-architecture-design]] — sleep stage structure that caused the gap
- [[2026-04-26-adaptive-stage-selection-design]] — the patch that made Stage B optional
- [[2026-04-26-sleep-stage-prediction-tonight]] — gap analysis that identified this
