---
title: Design — Context Persistence Across Restarts (Speaking Daemon)
aliases: [context-persistence, session-persistence, layer1-layer2]
tags: [reference, design]
created: 2026-04-24
---

# Design — Context Persistence Across Restarts (Speaking Daemon)

> **tl;dr** IMPLEMENTED (2026-04-24 23:14) but Layer 2 turn-log path is silently broken. Two-layer persistence for [[alice-speaking]] session continuity: Layer 1 saves `session_id` to disk after every turn (primary, working correctly); Layer 2 bootstraps from turn_log when Layer 1 fails (fallback, **broken** — see Known Bugs). Part of [[design-unified-context-compaction]].

## Root Cause

`self.session_id` is initialized to `None` in `SpeakingDaemon.__init__`. After a restart it's always `None` until the first turn completes. Any surface that fires between restart and the first inbound message lands in a fresh session.

## Two-Layer Persistence Design

### Layer 1 — persist session_id (primary restart path)

**Persist:** After every `ResultMessage`, if `msg.session_id` is not None, write:

```json
{ "session_id": "abc123...", "saved_at": "2026-04-24T12:55:26-04:00" }
```

to `inner/state/session.json`.

**Reload on startup:** In `__init__`, read `inner/state/session.json`. If present and parseable, pre-populate `self.session_id`. `_build_options()` already passes `resume=self.session_id` — no other change needed.

**Preflight check (optional but recommended):** Before the first `resume=` turn, verify that `cfg.work_dir / ".claude/sessions" / f"{self.session_id}.jsonl"` exists. If the file is gone, clear `self.session_id` and let Layer 2 handle recovery. This avoids a wasted API call if sessions were manually cleared.

**Layer 1 failure handling:** Wrap the first turn after a reload in a try/except for session-not-found errors from the SDK. On failure, clear `self.session_id` and log `session_resume_failed` to events.jsonl, then retry the same prompt without `resume=` — this triggers Layer 2 on the retry.

### Layer 2 — bootstrap from turn_log (fallback only)

Triggered when `self.session_id is None` at startup (Layer 1 didn't load or failed).

On daemon start, if `render_for_prompt()` returns a non-empty string, compose a bootstrap turn:

```
[Daemon restart — context restoration from turn log]

Recent conversation ({n} turns):
{render_for_prompt(self.turns.tail(cfg.context_bootstrap_turns))}

Resume naturally.
```

Send this as the first turn. The resulting `session_id` becomes the active session. All subsequent turns (inbound + surface) share it.

**Bootstrap turn format.** No tools, no Signal output — silent re-orientation. The return value is discarded. Emit `context_bootstrap` event to events.jsonl for observability.

If `render_for_prompt()` also returns empty (first boot, no turn history): start fresh, no recovery needed.

### New config key

```json
"context_bootstrap_turns": 20
```

How many recent turns to include in the Layer 2 bootstrap. Default 20 covers ~10 exchanges. Hot-reloadable.

## Implementation Steps (from master checklist)

- ✅ **1. Persist session_id to `session.json`** — *Confirmed: `self._session_path` wired in `__init__`, written after each `ResultMessage`.*
- ✅ **2. Load session on startup** — *Confirmed: `__init__` reads `session.json`, verifies SDK session still exists via `session_state.sdk_session_exists()`, sets `self.session_id`.*
- ✅ **3. Layer 1 failure handling** — *Confirmed: `_run_turn()` detects missing-session error, clears `self.session_id`, calls `_prime_bootstrap_preamble()` for Layer 2 fallback.*
- ✅ **4. Wire up `render_for_prompt()`** for Layer 2 bootstrap — *Confirmed: `turn_log.render_for_prompt()` + `compaction.build_bootstrap_preamble()` called by `_prime_bootstrap_preamble()`; `context_bootstrap_turns` config used.*

**ALL ITEMS IMPLEMENTED** — confirmed by source read of `daemon.py` + `compaction.py` + `turn_log.py` at 2026-04-24 23:14.

## Known Bugs (as of 2026-04-26)

### Layer 2 turn-log path is silently broken

Traced in detail at [[2026-04-26-context-bootstrap-full-trace]] (wake 101, 2026-04-26 08:43 EDT).

`turn_log.render_for_prompt()` skips any turn with `outbound=None`. In the v3 unified-context design, Speaking Alice sends replies by calling the `send_message` MCP tool — the daemon's `_handle_signal()` stores the turn with `outbound=None` always. Result: **every v3 turn has `outbound=None`, so `render_for_prompt()` returns an empty string for all turns.** The Layer 2 turn-log bootstrap composes a preamble with no content — effectively a no-op.

Layer 2 has two sub-cases:
- **Summary path** (compaction summary exists): `build_bootstrap_preamble()` injects the summary text verbatim. This part works. The tail-turns section is empty (broken), so only the summary is injected.
- **Turn-log path** (no summary, or no turns): preamble is entirely empty → fresh session, no context recovery.

Fix: store the last outbound text from `_send_message()` in `_turn_last_outbound`, attach it in the turn log's finally block. See [[2026-04-25-layer2-turnlog-coldstart-fix]] for the proposed 4-line change. Both this fix and the compaction fix are part of the pending daemon.py bundle.

**Practical impact today:** Layer 1 is working — `session.json` is written after every turn, SDK JSONL is replayed on warm resume. Layer 2 is the fallback that fires only when the JSONL file is gone. As long as session files aren't purged, cold restarts are effectively Layer 1. The bug is latent — it fires on container reprovisioning or manual session clear.

## Related

- [[design-unified-context-compaction]] — parent design; all three problems and how they compose
- [[design-context-compaction]] — Problem 2 (token threshold + session roll)
- [[design-outbox-decoupling]] — Problem 3 (explicit send_message)
- [[alice-speaking]] — the daemon this modifies
- [[alice-core]] — `alice_core.session` module owns `session.json` r/w/clear + SDK JSONL existence preflight (Layer 1 implementation home)
- [[claude-agent-sdk]] — `resume=` mechanics, local JSONL session files
- [[2026-04-26-context-bootstrap-full-trace]] — full source trace of Layer 1 + Layer 2 mechanics (wake 101)
- [[2026-04-25-layer2-turnlog-coldstart-fix]] — proposed fix for outbound=None bug
- [[2026-04-26-compaction-never-fires]] — companion bug: compaction hasn't fired, so no summary exists for summary path
