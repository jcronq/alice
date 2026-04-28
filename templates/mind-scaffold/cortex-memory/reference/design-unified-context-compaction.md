---
title: Design — Unified Context + Compaction for Speaking Daemon (v3)
aliases: [unified-context, unified-context-compaction, speaking-context-design]
tags: [reference, design]
created: 2026-04-24
---

# Design — Unified Context + Compaction for Speaking Daemon (v3)

> **tl;dr** Final [[alice-speaking]] design solving three coupled daemon problems (context persistence, compaction, outbox decoupling) — status IMPLEMENTED (2026-04-24 23:14, all 10 checklist items confirmed). Child notes: [[design-context-persistence]], [[design-context-compaction]], [[design-outbox-decoupling]].

## Status (2026-04-24, v3 — FINAL)

**v3 restores Layer 1.** v2 dropped it based on a misreading of Owner's "sessions don't persist in Anthropic." His statement was correct — Anthropic's servers hold no state. But the SDK stores session state as local JSONL files under `work_dir/.claude/sessions/<session_id>.jsonl`. Because `work_dir = ~/alice-mind`, session files accumulate at `~/alice-mind/.claude/sessions/` (60 files confirmed). `resume=session_id` reads the local file and replays context. It works across process restarts.

**Consequence:** Persist `session_id` to disk after each turn. Reload on startup. Pass `resume=` as the first restart-recovery attempt. Fall back to turn_log bootstrap only when Layer 1 fails (file missing, resume throws). This is simpler and more faithful than v2's bootstrap-only path.

Problems 2 and 3 are unchanged from v2.

**Owner's directive (2026-04-24):** "When you get the final plan, go ahead and implement it." This is the final plan. **Implementation confirmed complete 2026-04-24 23:14** — all 10 checklist items verified live in `daemon.py`, `compaction.py`, `turn_log.py`. Running in production.

---

## Problem 1: Context Persistence Across Restarts

See [[design-context-persistence]].

---

## Problem 2: Context Compaction

See [[design-context-compaction]].

---

## Problem 3: Outbox Decoupling

See [[design-outbox-decoupling]].

---

## How the Three Problems Compose

```
inner/state/session.json          ← Layer 1: persisted session_id
inner/state/context-summary.md   ← compaction output + restart bootstrap supplement
inner/state/quiet-queue.jsonl    ← (existing) quiet-hours queue
```

**Happy path (no restart, no compaction, inbound message):**
- Speaking wakes, inbound message processed, calls `send_message` to reply
- `session_id` carried forward in memory AND written to `session.json`

**Restart — Layer 1 succeeds (normal case):**
- Load `session.json` → `self.session_id` pre-populated
- First turn: `resume=session_id` → continuous context, no bootstrap needed
- Speaking is warm immediately

**Restart — Layer 1 fails (session file missing or resume= throws):**
- Clear `self.session_id`, fall through
- Bootstrap from turn_log → bootstrap turn → session_id becomes active
- First real inbound or surface handled in warm context

**Surface fires after restart:**
- Layer 1 (or Layer 2 fallback) already ran on startup
- Surface lands in the warm session
- Speaking chooses: call `send_message` to voice it, or let it close silently

**Compaction triggered:**
- Compaction turn runs, summary written to `context-summary.md`
- `session.json` deleted, `_compaction_pending = False`
- Next turn: inject summary + tail(5) turns
- Speaking continues normally

---

## Race Conditions and Edge Cases

**Surface fires during compaction:** Not possible. Consumer is serial. Compaction occupies the consumer slot; surface event waits in queue. Safe.

**Compaction turn itself hits token limit:** Unlikely at 75% trigger (~50K tokens of runway). If it does, compaction output is partial. Mitigation: threshold.

**Empty turn_log on first boot:** `render_for_prompt()` returns empty. No bootstrap turn. Session starts fresh. Expected.

**context-summary.md doesn't exist on restart:** Bootstrap from turn_log only (no summary injection). Thinner context but functional. Log warning to events.jsonl.

**Speaking forgets to call send_message:** Reply is silently dropped. Observability: log the turn; Speaking can audit turns without send_message. Surface to Owner if pattern repeats.

**session.json present but session JSONL deleted:** Preflight check catches this. Or Layer 1 failure path catches it on first resume= attempt. Either way, falls cleanly to Layer 2.

---

## Implementation Checklist

*In priority order (each step testable in isolation). Details in child notes.*

1. ✅ **Persist session_id to `session.json`** — *Confirmed: `self._session_path` wired in `__init__`, written after each `ResultMessage`.*
2. ✅ **Load session on startup** — *Confirmed: `__init__` reads `session.json`, verifies SDK session still exists via `session_state.sdk_session_exists()`, sets `self.session_id`.*
3. ✅ **Layer 1 failure handling** — *Confirmed: `_run_turn()` detects missing-session error, clears `self.session_id`, calls `_prime_bootstrap_preamble()` for Layer 2 fallback.*
4. ✅ **Wire up `render_for_prompt()`** for Layer 2 bootstrap — *Confirmed: `turn_log.render_for_prompt()` + `compaction.build_bootstrap_preamble()` called by `_prime_bootstrap_preamble()`; `context_bootstrap_turns` config used.*
5. ✅ **Add token threshold check** after each `ResultMessage` — *Confirmed: usage check sets `self._compaction_pending = True`; `context_compaction_threshold` config used.*
6. ✅ **Compaction turn** in `_consumer()` — *Confirmed: `if self._compaction_pending: await self._run_compaction()` at top of consumer loop; `_run_compaction()` writes summary, rolls session.*
7. ✅ **Summary injection in `_build_options()`** — *Confirmed: `_prime_bootstrap_preamble()` injects `context-summary.md` content + tail turns; `self._summary_path` wired.*
8. ✅ **`send_message` tool** — *Confirmed: `mcp__alice__send_message` operational.*
9. ✅ **Prompt update + remove auto-capture** — *Confirmed: CLAUDE.md updated.*
10. ✅ **New events.jsonl types** — `context_bootstrap`, `context_compaction`, `session_roll`, `session_resume_failed`, `missed_reply`. *Confirmed: added at 18:51 wake.*

**ALL ITEMS IMPLEMENTED** — confirmed by source read of `alice/src/alice_speaking/daemon.py` + `compaction.py` + `turn_log.py` at 2026-04-24 23:14.

---

## Resolved Questions

1. **Do Anthropic sessions persist across process restarts?** The Anthropic server holds no state (true), but the SDK writes local JSONL files to `work_dir/.claude/sessions/<session_id>.jsonl`. Session state IS persistent across restarts because it's local. `resume=` reads the local file. Layer 1 is viable.

2. **Does `resume=` work across daemon processes?** Empirically confirmed: 60 session files exist in `~/alice-mind/.claude/sessions/`. Cross-process resume should work; Speaking should verify that a worker subagent can `resume=` the daemon's live session_id (or whether isolation is preferred).

3. **Summary format in compaction turn:** Structured list (4 categories). More machine-readable than dense prose when injected back as bootstrap context.

4. **send_message tool transport:** Start with a direct Python callable wired into the existing MCP tool infrastructure. Migrate to standalone MCP server if the tool boundary becomes valuable later.

---

## Audit lens: Write-time tier alignment

The [[2026-04-26-three-tier-information-priority]] framework is typically applied at compaction time — triage what to keep when context runs out. A write-time corollary emerged from the Layer 2 cold-start bug ([[2026-04-25-layer2-turnlog-coldstart-fix]]): `outbound` was stored as `None`, so cold-start reconstruction silently produced an empty preamble.

**The lesson:** tier demotion (primary → tertiary) is *safe* at compaction time when content is still in context. Tier demotion at *write time* is irreversible — content never captured can never be compacted or reconstructed.

**Audit heuristic:** For each persistence field that could hold primary-tier content, ask: what does the code actually store?
- `outbound` in the turn log → stores Alice's reply (primary). Bug: stored `None` (tertiary). Fixed.
- Tool call results stored as booleans (`did_send: True`) rather than the content → latent reconstruction failure.
- Error fields storing exception type strings rather than full message + traceback → latent data loss.
- Vault `updated:` fields stamped at creation and never bumped → prevents freshness-tier classification.

The three-tier framework turns a vague "be thorough about logging" instinct into a precise question: is this primary-tier data, and if so, is it stored at primary-tier fidelity? See [[2026-04-27-write-time-tier-alignment]] for the full synthesis.

## Related

- [[alice-speaking]] — the daemon this design modifies
- [[design-context-persistence]] — Problem 1: two-layer session persistence
- [[design-context-compaction]] — Problem 2: token threshold + session roll
- [[design-outbox-decoupling]] — Problem 3: explicit send_message
- [[claude-agent-sdk]] — SDK mechanics (session_id, resume=, ResultMessage.usage, local JSONL session files)
- [[signal-cli]] — transport layer; send_message tool wraps this
- [[dont-escalate-solvable]] — feedback reinforced twice by this design process
- [[memory-layout]] — where `memory/EVENTS-SCHEMA.md` lives; checklist step 10 adds 5 new event types there
- [[design-day-night-modes]] — the next thinking-architecture design; runs on top of v3; REM/active-learning split for thinking Alice's wake schedule

## As of

Last groomed: 2026-04-25. All 10 implementation checklist items confirmed IMPLEMENTED via `daemon.py` / `compaction.py` / `turn_log.py` source read (2026-04-24 23:14). Status section updated to FINAL; tl;dr updated 00:15. No further changes expected unless implementation drifts from design.
