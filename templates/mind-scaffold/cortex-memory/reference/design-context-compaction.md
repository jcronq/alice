---
title: Design — Context Compaction (Speaking Daemon)
aliases: [context-compaction, session-roll, compaction-turn]
tags: [reference, design]
created: 2026-04-24
---

# Design — Context Compaction (Speaking Daemon)

> **tl;dr** IMPLEMENTED and WORKING as of 2026-04-26 10:20 EDT. Design: at 150K effective tokens, [[alice-speaking]] runs a silent summary turn, saves it to `inner/state/context-summary.md`, rolls the session, and injects the summary into the next fresh session. Compaction confirmed firing 4× since fix landed (commit 0a725a7). Part of [[design-unified-context-compaction]].

## Why It's Urgent

Unifying surfaces into the same session means context grows from two sources: Signal messages + surfaces. Without compaction, the session will blow out the context window over days.

## Trigger: Token Threshold Check

`ResultMessage.usage` is already emitted per turn. Add a per-turn check:

```python
if msg.usage and msg.usage.get("input_tokens", 0) > cfg.speaking.get("context_compaction_threshold", 150_000):
    self._compaction_pending = True
```

Sets a flag at the END of a turn. Compaction happens before the NEXT turn.

> ✅ **Bug fixed (2026-04-26 09:59 EDT):** The original `compaction.py` checked `usage["input_tokens"]` (raw, always 7–23) instead of effective context. Fix deployed in commit 0a725a7 — now correctly checks `cache_read + cache_creation + input_tokens`. Compaction first fired at 10:20 EDT; 4× total by midday. See [[2026-04-26-compaction-never-fires]] for the full bug history.

### New config key

```json
"context_compaction_threshold": 150000
```

For a 200K-token model, 150K is ~75% full — enough runway to run a compaction turn without hitting the limit mid-process. Hot-reloadable.

## Compaction Turn

Before the next event is dispatched, if `self._compaction_pending`:

1. **Run a compaction turn** (no output sent):
   ```
   [Internal — context compaction]

   Before we continue, summarize our conversation:
   1. Active threads: open questions, pending tasks Owner mentioned
   2. Owner's current state (mood, schedule, what he's working on)
   3. Recent surface verdicts that shaped your behavior
   4. Facts established here that aren't yet in cortex-memory

   Under 600 words. This becomes your bootstrap context after the session rolls.
   ```

2. **Capture summary** from assistant text response. Save to `inner/state/context-summary.md`.

3. **Session roll:** clear `self.session_id` and delete `inner/state/session.json`. Set `self._compaction_pending = False`. Emit `session_roll` event.

4. **Next turn starts fresh** with summary injected (see below).

## Summary Injection (Unified Restart + Compaction Path)

Both restart-bootstrap and post-compaction start a new session. Both use the same injection format. `_build_options()` gains a flag: `include_summary: bool`. When true:

```
[Context summary — session rolled after compaction / daemon restart]

{contents of inner/state/context-summary.md}

---
Recent turns:
{render_for_prompt(turns.tail(5))}
```

The last 5 verbatim turns bridge the gap between the summary cutoff and now.

## Session-Roll Mechanics

The Claude Agent SDK doesn't allow rewriting an existing session's history. In-place compaction is impossible. Session roll is the right architecture: extract semantic state, discard raw turns, inject state into a fresh session. Speaking doesn't sound amnesiac because the summary carries everything Owner would notice is missing.

## Implementation Steps (from master checklist)

- ✅ **5. Add token threshold check** after each `ResultMessage` — *Confirmed: usage check sets `self._compaction_pending = True`; `context_compaction_threshold` config used.*
- ✅ **6. Compaction turn** in `_consumer()` — *Confirmed: `if self._compaction_pending: await self._run_compaction()` at top of consumer loop; `_run_compaction()` writes summary, rolls session.*
- ✅ **7. Summary injection in `_build_options()`** — *Confirmed: `_prime_bootstrap_preamble()` injects `context-summary.md` content + tail turns; `self._summary_path` wired.*

**ALL ITEMS IMPLEMENTED** — confirmed by source read of `daemon.py` + `compaction.py` at 2026-04-24 23:14.

## Lessons from the field

Cross-referencing [[context-window-pressure-survey-2026]] (2026-04-25 survey of 9 frameworks + 2024–2026 papers) against Alice's design:

**Alice does well:**
- Structured summary prompt (5 explicit categories) — better than generic "summarize this" (Factory scored structured +0.35 vs. prose).
- Pre-next-turn trigger (not mid-tool-chain) — matches best-practice "fire at natural task boundaries."
- Sleep-time consolidation (thinking hemisphere) — the compaction survives partial context loss because thinking continuously updates the vault as a side-channel.

**Gap to watch:**
- Artifact tracking: Alice's summary tracks open questions and established facts, but not modified files or session artifacts. If speaking edits a file during a long session and then compacts, that fact should be in the summary explicitly. Low risk for now (speaking sessions rarely span multi-file edits), worth revisiting if that pattern grows.
- Compaction observability: no log entry emitted when compaction fires. Worth adding a `session_compaction` event to `memory/events.jsonl` for post-hoc debugging. Full 3-step spec (extended event fields, summary archive dir, optional compaction_summary event) at [[2026-04-25-compaction-event-observability]]. **Now unblocked** — v3 merged and compaction is firing in production. Low urgency but the tooling is ready.

## Dependencies

*Declared per [[2026-04-27-implicit-correctness-inheritance-design-principle]]: formal listing of foreign invariants this design inherits.*

| Dependency | Invariant assumed | Failure mode if invariant breaks | Detection signal |
|---|---|---|---|
| **Anthropic SDK caching model** | `cache_read_input_tokens + cache_creation_input_tokens + input_tokens` is the authoritative effective-context measure | `should_compact()` threshold fires at the wrong context pressure; either too early (false-positives) or never (context blowout) | Track `cache_read_input_tokens` per turn; alert if it's unexpectedly 0 for sessions known to have cache hits — may indicate API contract changed |
| **`ResultMessage.usage` field** | Every turn emits a `ResultMessage` with a populated `usage` dict | Token check silently skipped; compaction never fires; session blows out context | Add a `compaction_watcher` event to events.jsonl once per 24h confirming last compaction timestamp is within expected interval |
| **Session JSONL files** | `~/alice-mind/.claude/sessions/<id>.jsonl` persists across process restarts (SDK contract) | Layer 1 restart fails; falls back to bootstrap-only (Layer 2) which is degraded | `session.json` age check at startup: if session_id is stale, fall through to bootstrap path immediately rather than trying resume |

**Historical note:** Case 3 (caching model) was the root cause of the compaction-never-fires bug ([[2026-04-26-compaction-never-fires]]): `input_tokens` was checked alone (always 7–23 post-caching), missing the cache-read volume that dominates real context pressure. Fixed 2026-04-26 by summing all three fields.

## Related

- [[design-unified-context-compaction]] — parent design; all three problems and how they compose
- [[design-context-persistence]] — Problem 1 (session_id persistence, restart recovery)
- [[design-outbox-decoupling]] — Problem 3 (explicit send_message)
- [[alice-speaking]] — the daemon this modifies
- [[claude-agent-sdk]] — ResultMessage.usage, session mechanics
- [[context-window-pressure-survey-2026]] — 2026 survey: 9 frameworks, 6 strategy families, compaction turn best practices
- [[2026-04-26-compaction-never-fires]] — bug history: trigger checked wrong field; fixed 2026-04-26 09:59 EDT; confirmed working
- [[2026-04-25-compaction-event-observability]] — proposed additions to logging when compaction fires
- [[2026-04-27-implicit-correctness-inheritance-design-principle]] — the design principle motivating the Dependencies section above
