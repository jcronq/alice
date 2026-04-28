---
title: alice_core
aliases: [alice-core, alice_core, agentic kernel, agent kernel]
tags: [reference]
created: 2026-04-26
---

# alice_core

> **tl;dr** Shared kernel library used by both [[alice-speaking]] and [[alice-viewer]] (and `alice_thinking` one-shot wakes); owns the SDK lifecycle, OAuth auth, event logging, config primitives, and session-persistence helpers.

## Context

`alice_core` is one of four Python packages in the `/home/alice/alice/` monorepo (consolidated from separate repos in commit `a81b180`, 2026-04-24). It has no entry points of its own — pure library. Both hemispheres import from it rather than duplicating infrastructure code.

**Location:** `~/alice/src/alice_core/`

## Modules

| Module | Responsibility |
|--------|---------------|
| `kernel.py` | `AgentKernel` — drives one `sdk.query()` call end-to-end. Dispatches `AssistantMessage` blocks (text / tool_use / thinking) to handlers + emits observability events. Handles timeout wrapping and graceful cancellation. Knows nothing about Signal, quiet hours, or session persistence — those are handler concerns. |
| `events.py` | `EventLogger` (append-only JSONL writer) + `EventEmitter` protocol. Write failures are swallowed so observability never breaks the main loop. `CapturingEmitter` available for tests. Event taxonomy (names, fields) is defined by the hemisphere callers — this module is domain-agnostic. |
| `auth.py` | OAuth token loading. Consolidates two prior duplicate implementations (`alice_speaking.think._load_token` and `alice_speaking.config._load_env_file`). Resolution order: `CLAUDE_CODE_OAUTH_TOKEN` env var → `~/.config/alice/alice.env`. Exposes `find_token()` and `ensure_token()`. |
| `config.py` | Env + JSON config primitives with hemisphere-scoped sections. Stub as of 2026-04-26. |
| `session.py` | `session.json` read/write/clear + SDK-session JSONL existence preflight. Moved from `alice_speaking.session_state` during kernel refactor (re-export shim kept for backward compatibility). Allows both hemispheres to share session persistence without code duplication. |
| `sdk_compat.py` | Helpers that paper over SDK quirks across versions: `_short()` (truncates arbitrary values for log fields), `looks_like_missing_session()` (pattern-matches exception name+message to detect stale `resume=` without coupling to a specific SDK class). |

## AgentKernel — what it handles

- Building `ClaudeAgentOptions` from a typed `KernelSpec`.
- Dispatching SDK message blocks to observers + handlers.
- Emitting SDK-level events: `assistant_text`, `tool_use`, `thinking`, `user_message`, `result`, `system`.
- Timeout wrapping (`max_seconds > 0`) and graceful cancellation.
- Catching + emitting `exception` events without swallowing them.

### `_build_options()` — notable knobs

- **`max_buffer_size`** — set to `10 * 1024 * 1024` (10 MB) as of 2026-04-26. The SDK's `subprocess_cli.py` default is 1 MB (`_DEFAULT_MAX_BUFFER_SIZE`). Dense tool-use turns (many vault reads + long JSON response) can exceed 1 MB, causing `CLIJSONDecodeError`. Fix: pass `max_buffer_size=10*1024*1024` to `ClaudeAgentOptions`. Implemented in `alice/src/alice_core/kernel.py`; uncommitted (Owner owns commit). See [[2026-04-26-sdk-buffer-overflow-error]].

**Explicitly NOT handled by the kernel:** Signal I/O, quiet hours, surface watching, session persistence, context compaction, bootstrap preambles, missed-reply detection. These are composed by the speaking daemon via `BlockHandler` implementations.

## Monorepo context

The `alice` monorepo at `~/alice/` consolidates what were previously separate repos (`alice-speaking`, `alice-viewer`). Structure:

```
~/alice/
  src/
    alice_core/      ← this package
    alice_speaking/  ← Signal daemon
    alice_thinking/  ← one-shot thinking wake (python -m alice_thinking)
    alice_viewer/    ← FastAPI observability UI
  tests/             ← shared test suite (uv run pytest)
  bin/               ← host-side orchestration scripts (see table below)
  pyproject.toml     ← single project file; version 0.3.0
  uv.lock
```

### `bin/` — host-side orchestration scripts

Owner runs these on the host to manage Alice as a Docker system. Not in the container's PATH. Three are also baked into the container image via Dockerfile COPY.

| Script | What it does | Also in container? |
|--------|--------------|--------------------|
| `alice` | Run `claude` in the lease-holding worker (interactive or one-shot). Calls `alice-up` first. | No |
| `alice-client` | Send a message to Alice via `/state/alice.sock` (CLI transport); used by the `alice` wrapper and direct CLI turns. | No |
| `alice-up` | Start daemon + viewer + one worker slot (idempotent, 45s lease wait). | No |
| `alice-down` | Stop all Alice containers. `--rm` also removes them. | No |
| `alice-deploy` | Blue/green worker deploy, rollback, daemon update, or `all`. | No |
| `alice-init` | First-run setup: scaffold mind, write `alice.env`. One-time. | No |
| `alice-shell` | Drop into bash in the lease-holding worker or `daemon` container. | No |
| `alice-viewer` | Run the FastAPI observability UI on host port 7777 via `uv run alice-viewer`. | No |
| `alice-think` | Shim: `exec python -m alice_thinking "$@"`. Works inside container only (needs venv). | Yes → `/usr/local/bin/alice-think` (used by s6 alice-thinker service) |
| `alice-mind-autopush` | Auto-commit + push `alice-mind`. Runs from host systemd timer or s6 inside container. | Yes → `/usr/local/bin/alice-mind-autopush` (s6 alice-autopush) |
| `event-log` | Append structured event to `memory/events.jsonl`. | Yes → `/usr/local/bin/event-log` (shadowed by `alice-tools/bin/event-log` at runtime) |

See [[2026-04-26-alice-bin-scripts-audit]] for the full breakdown of how `alice/bin/` and [[alice-tools]]`/bin/` relate.

**PYTHONPATH:** `~/alice/src` — set in s6 run scripts and the `alice-think` shim.
**Venv:** `/opt/alice-venv` (was `/opt/alice-speaking-venv` before consolidation).
**Bind mount:** `${HOME}/alice → /home/alice/alice:rw` (single mount replaces old pair).

**README.md (as of 2026-04-27 ~11:50 EDT): Full rewrite complete, uncommitted.** The original README at `~/alice/README.md` was the initial extraction commit, never updated. Rewrite covers: blue/green worker architecture, daemon split, transport abstraction, hemisphere split, alice-viewer, current `alice-mind/` layout (cortex-memory, inner/notes, inner/surface, events.jsonl, HEARTBEAT.md), and current `bin/` wrappers. Owner's call on commit + push.

## Related

- [[alice-speaking]] — Signal daemon; imports `alice_core.kernel`, `alice_core.session`, `alice_core.auth`, `alice_core.events`
- [[alice-viewer]] — observability UI; in the same monorepo
- [[claude-agent-sdk]] — the underlying SDK the kernel drives
- [[design-context-persistence]] — Layer 1 + Layer 2 design that `alice_core.session` implements
- [[secrets-management]] — `alice_core.auth.find_token()` / `ensure_token()` documented here; `alice_core.secrets` wrapper proposed but unimplemented
- [[2026-04-26-alice-thinking-wake-implementation]] — code-level trace of `alice_thinking/wake.py`: entry point, config hot-reload path, flock singleton, fresh-context model launch

## Recent synthesis

*Night 1 Stage D synthesis — 2026-04-27 (bridge-linked 2026-04-28)*

- [[2026-04-27-bootstrap-recovery-gap-discovery-split]] — bootstrap recovery vs morning discovery: alice-core's cold-start sequence handles the recovery half; morning discovery is a separate gap
- [[2026-04-27-metadata-entropy-gap]] — metadata entropy gap: coordination mechanisms degrade when alice-core metadata has lower entropy than the content it coordinates
