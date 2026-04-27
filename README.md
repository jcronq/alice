# alice

A personal AI agent that lives in Docker, talks to you over Signal / a CLI
socket / Discord, remembers things in a git repo, and can ship code to her own
repos.

This repo is the **runtime** — the sandbox, the transport bridges, the
speaking + thinking hemispheres, the viewer, and the CLI wrappers. Your
agent's personality, memories, and skills live in a separate **mind repo**
(your own; created by `alice-init`).

## What's here

```
alice/
├── sandbox/               # Docker images + compose + entrypoint
│   ├── daemon/            # alice-daemon (signal-cli JSON-RPC, cron)
│   ├── worker/            # alice-worker (blue/green; runs Claude turns)
│   ├── viewer/            # alice-viewer (introspection UI on :7777)
│   ├── docker-compose.yml
│   └── entrypoint.sh
├── src/                   # Python source for the runtime
│   ├── alice_core/        # auth, paths, config helpers
│   ├── alice_speaking/    # inbound turn pipeline (per transport)
│   ├── alice_thinking/    # wake-cycle pipeline (active + sleep modes)
│   └── alice_viewer/      # introspection UI
├── speaking/              # transport implementations (signal, cli, discord)
├── viewer/                # viewer static assets
├── bin/                   # CLI wrappers (alice, alice-up, alice-init, …)
├── templates/
│   └── mind-scaffold/     # starter files for a fresh mind
├── config/
│   └── alice.env.example
└── docs/
```

## Quickstart

You'll need Docker (or Docker Desktop on macOS), `gh`, and `git`.

```bash
# 1. Clone this repo
git clone https://github.com/jcronq/alice.git ~/alice

# 2. Add the CLI to your PATH
export PATH="$HOME/alice/bin:$PATH"     # persist in your shell rc

# 3. First-run setup — scaffolds a mind repo and writes alice.env
alice-init

# 4. Register signal-cli (one time; interactive QR scan inside the daemon)
docker exec -it alice-daemon signal-cli \
    -a "$(. ~/.config/alice/alice.env; echo "$SIGNAL_ACCOUNT")" link -n "Alice"

# 5. Bring up the daemon + a worker (blue by default) + viewer
alice-up

# 6. Talk to her
alice                 # interactive (CLI transport, Unix-socket)
alice -p "ping"       # one-shot
# Or send her a Signal message from an allowed sender.
# Or DM/mention her in a Discord guild she's joined.
```

## Architecture

See `docs/ARCHITECTURE.md` for the full breakdown. Short version:

**Containers (compose):**

- `alice-daemon` — singleton. Runs signal-cli in JSON-RPC mode on
  port 8080 + cron for thinking-hemisphere wakes. No Claude here.
- `alice-worker-blue` / `alice-worker-green` — blue/green worker slots.
  Exactly one is live at a time, holding an exclusive `flock` on
  `/state/worker/lease`. The other waits. `alice-deploy` swaps them.
- `alice-viewer` — read-only introspection UI on `localhost:7777`. Shows
  turns, surfaces, notes, and vault state.

**Transports (pluggable, in `speaking/`):**

- **Signal** — inbound via signal-cli daemon JSON-RPC; outbound via the
  same. Allowlisted senders.
- **CLI** — Unix-socket transport at `/state/alice.sock`. `alice-client`
  speaks it. Used for interactive shell sessions and smoke tests.
- **Discord** — DM + guild support. Mention or DM the bot.

All transports share one inbound pipeline (`alice_speaking`). Outbound
replies are explicit: the agent calls the `send_message` MCP tool with
`recipient='self'` (reply on same channel) or a named recipient. Returning
text alone does NOT send.

**Hemispheres:**

- **Speaking** — fires per inbound turn. Sees the user's message, decides
  what to do, replies (or doesn't). Can run subagents to build/edit/deploy.
- **Thinking** — fires on cron (~5 min). Drains `inner/notes/`, grooms the
  vault, runs research from `inner/ideas.md`, surfaces actionable findings
  back to Speaking via `inner/surface/`. Two modes: **active** (07:00–23:00)
  and **sleep / REM** (23:00–07:00, with consolidation / downscaling /
  recombination sub-stages).

**Volumes (host → container):**

- `~/alice-mind` rw — her mind (memory, skills, identity).
- `~/alice-tools` rw — your sidecars (smart home, AV, repo helpers, …).
- `~/alice` rw — this repo, mounted live so subagents can self-improve.
  Hemisphere boundary: thinking MUST NOT write here.
- `~/.claude` ro — host Claude config (directory-mounted so atomic
  credential refreshes propagate; entrypoint symlinks the right files).
- `~/.local/state/alice` rw → `/state` — runtime state (worker lease,
  daemon logs, viewer cache, transport sockets).

## Mind repo

Every Alice has her own mind. By default `alice-init` scaffolds one at
`~/alice-mind`. You can instead:

- Clone an existing mind: `alice-init` → option 1 → paste the URL
- Point at an existing directory: `alice-init` → option 2 → enter the path

The mind is a regular git repo. The `alice-mind-autopush` service commits
and pushes every 15 minutes if there are changes, so her memory is
versioned on whatever remote you configure (if any).

**Personalize:**

- `IDENTITY.md` — what kind of entity she is
- `CLAUDE.md` — operating rules, memory protocol, skills index
- `USER.md` — about you
- `HEARTBEAT.md` — scheduled proactive checks (cron-style prompts)
- `.claude/skills/` — deterministic workflows she auto-invokes
- `cortex-memory/` — the groomed Obsidian-compatible vault she builds
  over time (atomic notes, wikilinks, dated dailies, conflicts log)
- `inner/notes/`, `inner/surface/` — the messaging buses between
  hemispheres (notes Speaking → Thinking, surfaces Thinking → Speaking)
- `memory/events.jsonl` — structured event stream (meals, workouts,
  weight changes, errors) for queryable history

## Bin wrappers

```
alice              # interactive CLI client (Unix-socket transport)
alice -p "..."     # one-shot CLI prompt
alice-client       # raw socket client (used by alice)
alice-up           # bring up daemon + active worker slot + viewer
alice-down         # tear it all down
alice-deploy       # blue/green swap (build new image, swap live slot)
alice-shell        # docker exec into the live worker
alice-think        # manually trigger a thinking wake
alice-init         # first-run scaffold (creates / clones mind, writes env)
alice-mind-autopush  # autopush daemon (runs inside the worker)
event-log          # tail / query memory/events.jsonl
```

## Sidecars

If you want Alice to control your smart home, your AV stack, your repos,
whatever — drop those scripts in `~/alice-tools/`. They're mounted at
`/home/alice/alice-tools/` inside the worker and on PATH. Or extend the
container further via a `docker-compose.override.yml` in `sandbox/`.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

This project is not currently accepting external contributions; please open
an issue rather than a PR. If contributions are opened up later, contributors
will be asked to sign a [CLA](CLA.md). See [CONTRIBUTING.md](CONTRIBUTING.md)
for details.
