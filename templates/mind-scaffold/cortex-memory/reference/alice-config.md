---
title: alice-config
aliases: [alice-config, alice.config.json, runtime config]
tags: [reference]
created: 2026-04-26
---

# alice-config

> **tl;dr** `~/alice-mind/config/alice.config.json` — Alice's behavioral knobs. Hot-reloaded at event boundaries for most speaking fields; cadence fields take effect on the next timer loop for thinking.

## File location

`/home/alice/alice-mind/config/alice.config.json`

Loaded at startup and re-checked on each event boundary (speaking daemon). Thinking's s6 timer loop re-reads cadence values at the start of each iteration, so cadence changes take effect within one cycle. Writing is done atomically via tempfile-replace.

Speaking Alice can self-tune via `read_config` / `write_config` MCP tools. Owner can edit the file directly on the host or inside the container (host side is safer for persistence — container filesystem is ephemeral).

## Schema

### `speaking` section

Controls [[alice-speaking]] daemon behavior. Deep-merged over `SPEAKING_DEFAULTS` in `config.py`.

| Key | Default | Current | Notes |
|-----|---------|---------|-------|
| `model` | `claude-opus-4-7` | *(default)* | LLM for all signal turns. Hot-reloadable ✅ |
| `always_thinking` | `true` | *(default)* | Extended thinking on/off. Restart required ⚠️ |
| `working_context_token_budget` | `2000` | `50000` | Thinking token budget passed to Claude Code. Hot-reloadable ✅ |
| `rate_limit_policy.retry` | `true` | *(default)* | Auto-retry on rate limit. Restart required ⚠️ |
| `rate_limit_policy.notify_user_after_seconds` | `30` | *(default)* | Notify Owner after N seconds of rate-limit wait. Restart required ⚠️ |
| `proactive_messages_allowed` | `true` | *(default)* | Allow outbound without an inbound trigger. Restart required ⚠️ |
| `quiet_hours.start` | `"22:00"` | *(default)* | Quiet window open (local time). Hot-reloadable ✅ |
| `quiet_hours.end` | `"07:00"` | *(default)* | Quiet window close (local time). Hot-reloadable ✅ |
| `quiet_hours.timezone` | `"America/New_York"` | *(default)* | Timezone for quiet hours. Hot-reloadable ✅ |
| `context_bootstrap_turns` | `20` | *(default)* | Turns from `speaking-turns.jsonl` injected on Layer 2 cold start. Hot-reloadable ✅ |
| `context_compaction_threshold` | `150000` | *(default)* | Effective token count (input+cache_read+cache_create) that triggers compaction. Hot-reloadable ✅ |

**Note on compaction threshold:** As of 2026-04-26, `should_compact()` checks only `input_tokens` (always 7–23 for Signal turns) instead of effective tokens — compaction has never fired. Fix is pending in the daemon.py bundle. See [[2026-04-26-compaction-never-fires]].

### `thinking` section

Controls [[alice-speaking]]'s thinking hemisphere (`wake.py` + s6 timer loop).

| Key | Default | Current | Notes |
|-----|---------|---------|-------|
| `model` | `claude-sonnet-4-6` | *(default)* | LLM for thinking wakes. Applied at next wake. |
| `max_wake_seconds` | `0` (no timeout) | *(default)* | Hard wall clock limit per wake (0 = unlimited). Applied at next wake. |
| `allowed_tools` | `Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch` | *(default)* | Tools available to thinking. Applied at next wake. |
| `rem_cadence_minutes` | `5` | `5` | Sleep-mode (23:00–07:00) timer interval. Hot-reloadable ✅ (next loop iteration) |
| `active_cadence_minutes` | `30` | `5` | Active-mode (07:00–23:00) timer interval. Hot-reloadable ✅ (next loop iteration) |

**Note on active cadence:** Design spec ([[design-day-night-modes]]) specifies 30 min. Owner overrode to 5 min via config. This means active mode runs at the same cadence as sleep mode, accumulating many more wakes per day than originally designed.

## How hot-reload works

The speaking daemon calls `_maybe_reload_config()` at each event boundary (before processing each signal turn, surface, or emergency). It compares `alice.config.json`'s mtime; if changed, it calls `config.load()` and deep-merges the new `speaking` section. Takes effect immediately for the event being processed.

Hot-reloadable fields (per `daemon._maybe_reload_config` docstring):
- `model`, `quiet_hours`, `working_context_token_budget`, `context_bootstrap_turns`, `context_compaction_threshold`

Fields that require a daemon restart (set at init time):
- `always_thinking`, `rate_limit_policy`, `proactive_messages_allowed`

The thinking cadence (`rem_cadence_minutes`, `active_cadence_minutes`) is read by the bash s6 script at the top of each loop — no Python restart needed; effective within one cycle.

## `write_config` tool

Speaking can self-tune via the `write_config` MCP tool. It accepts a JSON patch string and deep-merges it:

```
write_config(patch='{"speaking":{"quiet_hours":{"start":"23:00"}}}', reason="extending quiet hours")
```

The tool's built-in docstring lists "model, quiet_hours, allowed_tools" as hot-reloadable. This is **accurate but easily misread**:
- `allowed_tools` refers to **thinking's** `thinking.allowed_tools` config key — which IS hot-reloadable (read fresh at each wake start in `wake.py:95–96`).
- **Speaking has no `allowed_tools` config concept.** Speaking's tool list (`builtin_tools + self.custom_tool_names`) is fixed at daemon init (`daemon.py:151`). There is no config key that controls it.

`write_config` performs **no key validation** — it deep-merges whatever patch it receives and stores it. If Speaking writes `{"speaking":{"allowed_tools":[...]}}`, the key will persist in the file, the daemon will reload the config, but `self.custom_tool_names` will never update (init-only). The change is silently dropped with no error or warning.

## Related

- [[alice-speaking]] — daemon that consumes this config
- [[design-day-night-modes]] — specifies cadence design; active-cadence default is 30 min
- [[design-unified-context-compaction]] — `context_compaction_threshold` schema
- [[design-context-persistence]] — `context_bootstrap_turns` schema
- [[2026-04-26-compaction-never-fires]] — bug: threshold never triggers due to wrong field check
- [[2026-04-26-speaking-mcp-tools-inventory]] — full tool list including `read_config`/`write_config`
- [[secrets-management]] — `alice.env` for secrets (credentials stay out of this file)
