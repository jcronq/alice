# Architecture

## Three-repo split

```
┌─────────────────────────────────────────────────────────────────┐
│  alice (this repo) — the runtime                                │
│                                                                  │
│   sandbox/         → Dockerfile, compose, s6 services           │
│   bin/             → host CLIs (alice, alice-up, alice-init, …) │
│   signal/          → bridge script (baked into image)           │
│   templates/       → scaffold for new minds                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ mounts rw at runtime
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  <user>-mind — her personality + memory (per user)              │
│                                                                  │
│   IDENTITY.md, CLAUDE.md, USER.md   — system prompt pieces      │
│   memory/                            — daily logs + events      │
│   .claude/skills/                    — deterministic workflows  │
│   .claude/sessions/                  — conversation transcripts │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ optional — personal tools
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  <user>-tools — personal sidecars (per user)                    │
│                                                                  │
│   whatever-cli/                                                 │
│   docker-compose.override.yml  — adds mounts/env to the sandbox │
└─────────────────────────────────────────────────────────────────┘
```

The runtime has zero user-specific config. Everything about "who Alice is
working for" lives in the mind repo. Everything about "what Alice can
poke at outside herself" lives in the optional tools repo.

## Runtime processes (inside the container)

s6-overlay supervises these long-run services:

| Service          | What it does                                               |
|------------------|------------------------------------------------------------|
| `signal-daemon`    | `signal-cli --http 127.0.0.1:8080` — the phone line        |
| `alice-speaking`   | Python daemon that routes inbound messages to claude       |
| `alice-thinker`    | Wake-driven proactive turns                                |
| `alice-autopush`   | Every 15 min, commits + pushes any changes in `alice-mind` |
| `alice-gh-watcher` | Polls watched GitHub repos, drops PR activity into `inner/notes/` |

Each has `run` and `finish` scripts under `sandbox/s6/<service>/`.

## Message flow

```
User's phone
   │ Signal protocol
   ▼
signal-cli daemon (in container, :8080)
   │ logs JSON envelope to signal-daemon.log
   ▼
alice-speaking daemon (in container)
   │ tails log, parses envelope
   │ looks up per-sender session pointer
   ▼
claude (Agent SDK, --resume <session-id>)
   │ reads alice-mind/ (CLAUDE.md + skills + memory)
   │ generates response
   ▼
alice-speaking daemon
   │ POSTs to signal daemon's JSON-RPC
   ▼
signal-cli daemon
   │
   ▼
User's phone
```

## Volumes at a glance

All state lives on the host; the container is ephemeral.

- Mind + tools + `~/.config/alice` + auth → mounted rw
- `gh` config, ssh keys, claude credentials → mounted ro
- `~/.alice-claude` holds claude's own session transcripts (in-repo via a
  symlink trick inside the container)

Rebuilding or recreating the container loses nothing. `docker compose
down --rm` is safe; `rm -rf ~/alice-*` is not.

## Why this split

- **Forkable.** Someone else can use the runtime without touching the mind.
- **Versionable.** The mind is a git repo. She evolves over commits.
- **Transferable.** rsync the `alice-mind/` (+ a few state dirs) to a new
  box, `docker compose up`, she's running there. No image rebuild needed
  unless the runtime itself changed.
- **Privacy.** The mind + tools are private; only the runtime could ever
  go public. Secrets (API keys, phone numbers, sheet IDs) live in config
  files and mounted state, not in the runtime.

## Scaling beyond one user

Not a goal. One container per agent, one agent per human. Alice is an
individual, not a service.
