# Components

Per-package documentation. One file per top-level package under `src/alice_*`. The stubs in this directory are placeholders — each gets filled in as the corresponding package is touched.

## Files

- `alice_speaking.md` — the Speaking hemisphere (user-facing daemon, turn handling, MCP tooling).
- `alice_thinking.md` — the Thinking hemisphere (background reasoning, vault grooming, wake loop).
- `alice_sm.md` — the idea state machine.
- `alice_watchers.md` — repository / event watchers that surface work to Thinking and Speaking.

Other packages (`alice_core`, `alice_daemon`, `alice_eval`, `alice_indexer`, `alice_metrics`, `alice_pi`, `alice_prompts`, `alice_skills`, `alice_viewer`) don't have stubs yet — add one here the first time you write a PR that needs to point at one.
