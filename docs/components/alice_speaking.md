# alice_speaking

**TBD** — Speaking is the user-facing hemisphere. It owns the Signal-driven turn loop, MCP tool surface, worker subagent dispatch, and the auto-fix issue-handling pipeline. Source: `src/alice_speaking/`.

Filled in as PRs touch the package. Points worth covering when this stub becomes real prose:

- turn lifecycle (inbound → Claude → outbound, `send_message` contract)
- MCP tool surface exposed to the Speaking model
- worker subagent dispatch protocol
- relationship to Thinking via `inner/notes/` and `inner/surface/`
