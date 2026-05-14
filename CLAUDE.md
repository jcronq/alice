# CLAUDE.md

Operating notes for agents working in this repo.

## What this repo is

The **runtime** for Alice — a personal AI agent that runs in Docker, speaks
over Signal / Discord / a CLI socket, and stores her mind (memory, skills,
identity) in a separate git repo. This repo holds the sandbox images,
transports, speaking/thinking pipelines, viewer, and CLI wrappers. See
`README.md` for architecture and `docs/ARCHITECTURE.md` for the deeper
breakdown.

### Where mind + tools live

By default the install puts both **inside this repo, under `data/`**:

- `data/alice-mind/` — Alice's mind (its own git repo, scaffolded by
  `alice-init`). Override with `ALICE_MIND=…` in the environment.
- `data/alice-tools/` — host-side sidecar scripts mounted into the
  worker on PATH. Override with `ALICE_TOOLS=…`.

`data/` is gitignored from this runtime repo, so neither shows up in
`git status` here. Inside the container both are still mounted at
their canonical paths (`/home/alice/alice-mind`, `/home/alice/alice-tools`).

## Talking to the running agent: `bin/alice`

`bin/alice` is the canonical way to interact with the live Alice agent
from this host. It docker-execs into whichever worker container currently
holds the message-processing lease and speaks the CLI transport socket
inside the sandbox — so the call hits the real running agent (mind repo,
session continuity, MCP tools), not a fresh `claude` subprocess.

```bash
bin/alice                    # interactive REPL
bin/alice "what's on today?" # one-shot prompt (bare arg promoted to -p)
bin/alice -p "ping"          # one-shot, explicit
bin/alice --json -p "..."    # raw JSON event stream — use this when an
                             # agent is driving Alice programmatically
```

Notes:

- `bin/alice` will run `bin/alice-up` first to make sure the daemon and a
  worker slot are live. If the sandbox isn't up yet, the first call may
  take a moment.
- `--json` emits one JSON event per line (`chunk`, `tool_use`, `done`,
  `error`). Prefer this when you're an agent capturing Alice's reply for
  further processing — it's stable and parseable.
- Exit codes (from `alice-client`): `0` success, `1` transport error,
  `2` Alice replied with `type=error`, `3` client error (bad args, socket
  missing).
- This is different from running `claude` directly: `claude` would spawn
  a fresh agent with no Alice context. Always go through `bin/alice` when
  you want to talk to *Alice*.

## Other bin wrappers

`bin/alice-up`, `bin/alice-down`, `bin/alice-deploy` (blue/green swap),
`bin/alice-shell` (exec into the live worker),
`bin/alice-speaking-bounce` (rescue tool — reap orphan claude procs and
`s6-svc -r` the speaking daemon when the CLI socket goes deaf),
`bin/alice-think` (trigger a thinking-hemisphere wake), `bin/alice-init`
(first-run scaffold), `bin/event-log` (tail/query `memory/events.jsonl`).

## Stage B (Consolidation) — google-adk workflow

Stage B sleep wakes can route through a typed-step workflow built on
google-adk's `SequentialAgent` / `ParallelAgent` instead of the
prompt-driven `sleep-b.md` path. Two flags in `alice.config.json`
under `thinking`:

- `stage_b_workflow_enabled` (default `false`) — flip the cutover.
- `stage_b_shadow_enabled` (default `false`) — run the workflow in
  shadow alongside the prompt path with `apply_writes=False`;
  telemetry tagged `stage_b_shadow_*`.

LLM calls dispatch through google-adk's `LiteLlm` adapter to the
local Qwen endpoint (`http://10.20.30.177:8033/v1`). Per-step
timeouts, error containment, and structured telemetry come for free
from the workflow shape.

Design: `docs/designs/stage-b-adk-workflow-sketch.md`. Cutover
protocol + flag-flip steps: `docs/designs/stage-b-cutover.md`.
