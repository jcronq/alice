# alice

A personal AI agent that lives in a Docker container, messages you over
Signal, remembers things in a git repo, and can push to its own repos.

This repo is the **runtime** — the sandbox, bridge, autopush, and CLI
wrappers. Your agent's personality, memories, and skills live in a
separate **mind repo** (your own; created by `alice-init`).

## What's here

```
alice/
├── sandbox/       # Docker image + compose + s6 services
├── bin/           # CLI wrappers (alice, alice-up, alice-init, …)
├── signal/        # signal-bridge.sh (baked into image)
├── templates/
│   └── mind-scaffold/   # starter files for a fresh mind
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

# 4. Register signal-cli (one time; interactive QR scan)
signal-cli -a "$(. ~/.config/alice/alice.env; echo "$SIGNAL_ACCOUNT")" link -n "Alice"

# 5. Start the container
alice-up

# 6. Talk to her
alice                 # interactive
alice -p "ping"       # one-shot
# Or send her a Signal message from an allowed sender.
```

## Mind repo

Every Alice has her own mind. By default `alice-init` scaffolds one at
`~/alice-mind`. You can instead:

- Clone an existing mind: `alice-init` → pick option 1 → paste the URL
- Point at an existing directory: `alice-init` → option 2 → enter the path

The mind is a regular git repo. The `alice-mind-autopush` service inside
the container commits + pushes every 15 minutes if there are changes, so
her memory is versioned on whatever remote you configure (if any).

Personalize:
- `IDENTITY.md` — what kind of entity she is
- `CLAUDE.md` — operating rules, memory protocol, skills index
- `USER.md` — about you
- `.claude/skills/` — deterministic workflows she auto-invokes

## Architecture

See `docs/ARCHITECTURE.md` for the detailed breakdown. The short version:

- One persistent Docker container (`alice-sandbox`), supervised by
  `s6-overlay`.
- Three s6 services inside: `signal-daemon` (signal-cli), `signal-bridge`
  (routes messages → claude), `alice-autopush` (commits her mind every 15m).
- Volumes mount her mind, her tools, her config from the host. Everything
  she writes persists on host.
- Auth: the host's `gh` token + claude credentials are bind-mounted read-
  only.

## Sidecars

If you want Alice to control your smart home, your AV stack, your repos,
whatever — drop those scripts in `~/alice-tools/`. They're mounted at
`/home/alice/alice-tools/` inside the container and on PATH. Or extend the
container further via a `docker-compose.override.yml` in `sandbox/`.

## Licensing

Private. Not yet intended for public distribution.
