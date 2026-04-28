---
title: Claude Agent SDK
aliases: [claude-agent-sdk, agent sdk, claude_agent_sdk]
tags: [reference]
created: 2026-04-24
---

# Claude Agent SDK

> **tl;dr** Python SDK that subprocesses `claude` with OAuth auth, streaming structured messages — used to drive [[alice-speaking]]'s conversation turns.

## Context

The Claude Agent SDK (`claude_agent_sdk`, Python) is the runtime engine under [[alice-speaking]]. Rather than calling a REST API, it subprocesses the `claude` CLI and reads the streaming JSON protocol. Auth comes from the host's Max subscription via OAuth — no API key is needed, just the `CLAUDE_CODE_OAUTH_TOKEN` env var populated at daemon start.

GitHub: `https://github.com/anthropics/claude-agent-sdk-python`

**Installed version (2026-04-26):** `claude_agent_sdk 0.1.66` at `/home/alice/alice/.venv/lib/python3.12/site-packages/claude_agent_sdk/`. The `max_buffer_size` option in `ClaudeAgentOptions` is a real, documented field (not a workaround) — `types.py:1479`, read by `subprocess_cli.py:28,56-60`. Default is 1 MB; [[alice-core]] `_build_options()` now sets it to 10 MB. See [[2026-04-26-sdk-buffer-overflow-error]].

## How alice-speaking uses it

The daemon calls `query(prompt=..., options=...)`, which is an async generator. It yields structured message objects:

| Type | Purpose |
|------|---------|
| `AssistantMessage` | Contains content blocks (text, tool use, thinking). Also surfaces `error` field if the model hit rate limits or returned an error. |
| `ResultMessage` | End-of-turn summary: `session_id`, `num_turns`, `duration_ms`, `total_cost_usd`, `usage`. |
| `TextBlock` | Actual reply text extracted from `AssistantMessage.content`. |
| `ToolUseBlock` | Tool invocation extracted from `AssistantMessage.content`. |
| `ThinkingBlock` | Extended thinking output. |

## Session continuity

`ResultMessage.session_id` is stored on the daemon instance. Subsequent turns pass `resume=session_id` in `ClaudeAgentOptions`, which tells the SDK to continue the same conversation.

**Local session persistence (confirmed 2026-04-24):** Despite Anthropic's server holding no state, the SDK writes local JSONL files to `work_dir/.claude/sessions/<session_id>.jsonl`. 60 such files confirmed at `~/alice-mind/.claude/sessions/`. This means `resume=` is viable *across process restarts* — the daemon can reload `session_id` from disk on startup and pick up exactly where it left off. See [[design-context-persistence]] for the Layer 1 + Layer 2 implementation design.

`ResultMessage.usage` contains per-turn token counts. The compaction threshold check is meant to fire when effective context pressure crosses a configured limit. **Known bug (2026-04-26):** `should_compact()` in `compaction.py` checks only `usage["input_tokens"]` (7–23 for normal Signal turns) rather than the full effective context (`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`). Compaction has never fired in production. A three-line fix is pending Speaking dispatch. See [[2026-04-26-compaction-never-fires]] for the investigation and [[design-context-compaction]] or [[design-unified-context-compaction]] for the design.

## Options (`ClaudeAgentOptions`)

```python
ClaudeAgentOptions(
    model=cfg.speaking.get("model"),
    allowed_tools=builtin_tools + custom_tool_names,   # Bash, Read, Write, Edit, Glob, Grep + MCP
    mcp_servers=mcp_servers,
    cwd=str(cfg.work_dir),
    resume=session_id,   # omitted on first turn
)
```

## Auth setup

At daemon start, `alice-speaking` sets:

```python
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = cfg.oauth_token
```

The token lives in `~/.config/alice/alice.env`. The `_sanity.py` module provides an OAuth smoke test (`uv run python -m alice_speaking._sanity`).

## Related

- [[alice-speaking]] — the daemon that wraps this SDK
- [[design-unified-context-compaction]] — full v3 design (context persistence + compaction + outbox decoupling)
- [[design-context-persistence]] — Layer 1 (session_id to disk) + Layer 2 (turn_log bootstrap) implementation
- [[design-context-compaction]] — token threshold check, compaction turn, session roll mechanics
- [[2026-04-26-speaking-mcp-tools-inventory]] — full inventory of Speaking's 16 tools (6 built-in + 10 custom MCP)
- [[owner]] — built the integration
