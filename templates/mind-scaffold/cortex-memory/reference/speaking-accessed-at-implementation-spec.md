---
title: "Implementation spec — speaking_accessed_at field"
aliases: [speaking-accessed-at-spec, speaking-accessed-at-implementation]
tags: [reference, design, implementation, alice-speaking, memory-design]
created: 2026-04-26
related: [[design-retrieval-protocol]], [[2026-04-26-observer-contamination-in-metrics]], [[design-ops-archive]], [[2026-04-26-implementation-gap-audit]]
status: ready-to-ship
---

# Implementation spec — `speaking_accessed_at` field

> **tl;dr** One-line addition to `_bump_access()` in `alice/src/alice_speaking/tools/memory.py`. Writing `speaking_accessed_at` separates Speaking's reads from Thinking's reads, fixing the observer-contamination gap in archival scoring. No other files need changes.

## Context

`read_memory` already bumps `last_accessed` + `access_count` on every call. The problem: Thinking's grooming reads also write `last_accessed`, contaminating the signal. A note Thinking groomed 20 times but Speaking never touched looks "recently accessed" — and resists archival that it shouldn't resist.

**The fix:** add a `speaking_accessed_at` field that only `_bump_access()` writes (i.e., only Speaking reads via `read_memory` set it). Thinking does NOT write this field — so it remains clean.

Design rationale: [[2026-04-26-observer-contamination-in-metrics]], [[design-retrieval-protocol]] §Companion mechanisms.

---

## The exact change

**File:** `alice/src/alice_speaking/tools/memory.py`

**Function:** `_bump_access()` (lines 120–151 as of 2026-04-26 audit)

**Current code (lines 135–137):**

```python
today = datetime.date.today().isoformat()
new_fm = _update_fm_field(body_fm, "last_accessed", today)
# Increment access_count.
```

**Change to (add one line after line 136):**

```python
today = datetime.date.today().isoformat()
new_fm = _update_fm_field(body_fm, "last_accessed", today)
new_fm = _update_fm_field(new_fm, "speaking_accessed_at", today)
# Increment access_count.
```

That's the complete code change. `_update_fm_field` already handles "field not present → append" so no notes need pre-seeding with this field.

---

## No other code changes needed

- **Frontmatter template:** no template file to update — `_update_fm_field` appends the field if absent.
- **Thinking:** reads but does NOT call `_bump_access()`, so `speaking_accessed_at` is never set by Thinking. The separation is already architecturally enforced.
- **Archival:** `design-ops-archive` references `speaking_accessed_at` with a 60-day resistance rule. Once the field exists in the vault, the archival logic (when implemented) can read it directly. No changes to archival code needed now.

---

## Behavioral rule (no code — convention only)

Speaking should **prefer `read_memory` over plain `Read`** when accessing vault files (`cortex-memory/**/*.md`). Plain `Read` bypasses `_bump_access()` entirely and won't set `speaking_accessed_at`. For non-vault files (code, config, dailies not in cortex-memory), plain `Read` is fine.

This is already documented in [[design-retrieval-protocol]] §Companion mechanisms — but worth repeating here as the implementation note, since the code change is useless if Speaking reads vault files via plain `Read`.

---

## What this enables downstream

1. **Archival resistance scoring** — notes Speaking frequently reads resist archival even if `access_count` is inflated by Thinking grooming. The 60-day window in [[design-ops-archive]] becomes meaningful.
2. **Three-tier SP surfacing** — [[2026-04-26-three-tier-information-priority]] proposes that `priority: context` surface hints should prefer primary-tier notes (high `speaking_accessed_at`). Without this field, the tier distinction is impossible.
3. **Quality signal** — Thinking can identify notes Speaking has never retrieved (strong archival candidates) vs notes Speaking actively uses (load-bearing, protect).

---

## Estimated effort

2–3 minutes. One line added, one test read of a vault note to confirm the field appears in frontmatter.

---

## Related

- [[2026-04-26-implementation-gap-audit]] — full audit; this is Gap #2 (highest priority, smallest scope)
- [[design-retrieval-protocol]] — where `speaking_accessed_at` is specified
- [[2026-04-26-observer-contamination-in-metrics]] — the problem this fixes
- [[design-ops-archive]] — downstream consumer of the field
- [[2026-04-26-three-tier-information-priority]] — second downstream consumer
