---
title: Design — Outbox Decoupling via send_message (Speaking Daemon)
aliases: [outbox-decoupling, send-message-tool, explicit-send]
tags: [reference, design]
created: 2026-04-24
---

# Design — Outbox Decoupling via send_message (Speaking Daemon)

> **tl;dr** IMPLEMENTED (2026-04-24 23:14). [[alice-speaking]] no longer auto-captures final assistant text as Signal output; all outbound messages require an explicit `send_message` tool call, giving speaking intentional control over every reply. Part of [[design-unified-context-compaction]].

## Root Cause

The old daemon loop auto-captured the final assistant text and sent it to Signal — but only for inbound-triggered turns. When a surface fired, the response had no outbox: it went nowhere. This silently swallowed the v1 design proposal voicing.

## Design: Explicit send_message Tool

Add a tool that speaking invokes deliberately to send a Signal message. The daemon stops auto-capturing final text as output.

**Interface:**

```python
send_message(
    recipient: str,   # "owner" | "friend" | E.164 number
    message: str      # The text to send
) -> dict             # {"status": "sent", "timestamp": "..."}
```

**Daemon loop change:**
Remove the post-turn logic that extracts `result.output[-1].text` and sends it to Signal. All outbound Signal messages must now come through `send_message`.

**Speaking's turn choices (explicit, every turn):**
- Call `send_message` → message goes to Signal
- Call `kick_off_worker` → delegated task
- Call `append_note` → something for thinking
- Let the turn close without action → silent processing

## Migration Path

1. Ship `send_message` tool (daemon wires it, MCP or equivalent).
2. Update speaking's system prompt: "to send a reply, call `send_message`. Returning text alone no longer sends it."
3. Ship daemon change: remove auto-capture from `_run_turn()`.
4. Watch logs for missed-reply events in first 24h.

No flag needed. The tool is additive; the prompt change makes the intent explicit; the daemon change completes the switch.

## Implementation Steps (from master checklist)

- ✅ **8. `send_message` tool** — *Confirmed: `mcp__alice__send_message` operational.*
- ✅ **9. Prompt update + remove auto-capture** — *Confirmed: CLAUDE.md updated; daemon no longer extracts `result.output[-1].text` for Signal.*
- ✅ **10. New events.jsonl types** — `context_bootstrap`, `context_compaction`, `session_roll`, `session_resume_failed`, `missed_reply`. *Confirmed: added to EVENTS-SCHEMA.md at 18:51 wake.*

**ALL ITEMS IMPLEMENTED** — confirmed by source read of `daemon.py` + `compaction.py` at 2026-04-24 23:14.

## Known limitation — `outbound` field reliability (v3 transition)

During the v3 transition period, **`outbound: null` in `speaking-turns.jsonl` is not a reliable missed-message signal.** Speaking confirmed on 2026-04-25 that five replies from the ~21:00–22:02 window were sent successfully but the turn log showed `outbound: null` for all of them. Two possible causes:

1. The v3 unified-context refactor may not capture `send_message` tool calls into the `outbound` field consistently yet.
2. `Stream closed` errors on `send_message` can record the call internally but fail silently.

**Preferred debugging signal:** `tool_use:send_message` events in the same turn JSON (more reliable than the `outbound` field), or confirmation from Owner's next message. Do not treat `outbound: null` alone as evidence of a missed reply until v3 stabilizes. Full details: [[alice-speaking]] Observability caveats.

## Related

- [[design-unified-context-compaction]] — parent design; all three problems and how they compose
- [[design-context-persistence]] — Problem 1 (session_id persistence)
- [[design-context-compaction]] — Problem 2 (token threshold + session roll)
- [[alice-speaking]] — the daemon this modifies
- [[signal-cli]] — transport layer; `send_message` wraps this
