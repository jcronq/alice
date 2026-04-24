# alice-speaking

Alice's speaking hemisphere. The Python daemon that holds the conversation.

Built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), which subprocesses `claude` and inherits OAuth auth from the host's Max subscription — no API key required.

## What lives here

- The always-on daemon that reads from the `signal-cli` JSON-RPC endpoint, drives Alice's turns, and writes replies back.
- Her Signal I/O tools (`send_message`, `set_typing`, `send_reaction`).
- Her indirect channel to the thinking hemisphere (`read_directive`, `write_directive`, `append_note`, `read_notes`, `read_thoughts`, `resolve_surface`).
- Her memory I/O (`read_memory`, `write_memory`, graph traversal).
- Her self-tuning config surface.

Task subprocesses (when a user asks for deep work) are spawned from here but run under Claude Code CLI, not inside this process.

## What does NOT live here

- **alice-mind** (`~/alice-mind`) — Alice's persona, memory, inner/ comms, thoughts, config. This repo reads that one; it does not own it.
- **alice-tools** (`~/alice-tools`) — skills and CLIs available to Alice (CozyHem, theater, etc.). Those ride through Claude Code.
- **alice sandbox** (`~/alice`) — the Docker container, s6 services, entrypoint. This repo's code runs inside that container.
- **thinking Alice** — a cron-triggered `claude -p` invocation, not a service. Lives in alice-tools' bin + the container's crontab.

## Design reference

See `alice-mind/HEMISPHERES.md` for the full architecture (two hemispheres, surfacing, quiet hours, information processing, configurability).

## Development

```
uv sync                                 # install deps
uv run python -m alice_speaking._sanity # OAuth smoke test (requires ~/.config/alice/alice.env)
```

## Status

Scaffold only. Agent SDK + OAuth verified. Signal I/O, persona loading, and the daemon loop are next.
