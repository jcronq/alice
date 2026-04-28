# Alice's sandbox

Persistent Docker container Alice runs inside. One long-lived container,
commands via `docker exec`. Works on Linux and macOS with Docker Desktop.

## Why

Alice should feel like a separate entity. The sandbox gives her:

- Her own user (`alice` uid 1000 inside; round-trips to your host uid)
- Her own git identity (`Alice <alice@localhost>`)
- Her own filesystem view — she sees `/home/alice/alice-mind` and nothing
  else from your host except what's mounted
- Her own installed tools (node, claude, gh, git, ssh, curl, jq, signal-cli)
  supervised by s6-overlay (signal-daemon, alice-speaking, alice-thinker, alice-autopush)

Everything Alice writes to the mounted volumes persists on the host.

## Layout

```
alice/
├── sandbox/
│   ├── Dockerfile            # image definition
│   ├── docker-compose.yml    # volumes, networking, lifecycle
│   ├── entrypoint.sh         # runs at container start
│   ├── alice.gitconfig       # Alice's git identity (inside the container)
│   └── s6/                   # s6-overlay service definitions
├── bin/
│   ├── alice                 # host wrapper — docker exec claude inside
│   ├── alice-up              # idempotent: start container if not running
│   ├── alice-shell           # bash shell inside (for debugging)
│   ├── alice-down            # stop container (add --rm to also remove)
│   ├── alice-init            # first-run: scaffold a mind + alice.env
│   ├── alice-mind-autopush   # baked into image; auto-commit mind
│   └── event-log             # baked; append structured event
├── templates/mind-scaffold/  # starter files for `alice-init`
└── config/alice.env.example
```

## Volumes

| Host path                 | Inside container               | Mode | Purpose                                           |
|---------------------------|--------------------------------|------|---------------------------------------------------|
| `~/alice-mind`            | `/home/alice/alice-mind`       | rw   | Alice's brain — memories, skills, identity        |
| `~/alice-tools`           | `/home/alice/alice-tools`      | rw   | Optional personal sidecars (can be empty)         |
| `~/.config/gh`            | `/home/alice/.config/gh`       | ro   | GitHub token (via `gh auth git-credential`)       |
| `~/.ssh`                  | `/home/alice/.ssh`             | ro   | SSH keys for outbound ssh                         |
| `~/.config/alice`         | `/home/alice/.config/alice`    | ro   | `alice.env` — per-host runtime config             |
| `~/.local/share/signal-cli` | `~/.local/share/signal-cli`  | rw   | Signal account registration (keys, avatars)      |
| `~/.local/state/alice`    | `~/.local/state/alice`         | rw   | Bridge session pointers + logs                    |
| `~/.alice-claude`         | `/home/alice/.claude`          | rw   | Claude Code session state                         |
| `~/.claude/.credentials.json` | same path in container     | ro   | Claude OAuth token                                |
| `~/.claude.json`          | same path in container         | ro   | Claude settings                                   |

## Network

The container is on the default Docker bridge; no ports are published.
signal-cli runs inside the container — port 8080 is internal only. For
outbound reach to host services, `host.docker.internal` resolves to the
bridge gateway (works on Linux + Docker Desktop).

## Lifecycle

```bash
alice-up          # create + start (idempotent)
alice             # interactive chat with claude
alice -p "ping"   # one-shot prompt
alice-shell       # bash inside the container
alice-down        # stop (state preserved)
alice-down --rm   # stop + remove container (volumes persist)
```

The container is `restart: unless-stopped`, so it comes back after reboots.

## Rebuilding

```bash
cd ~/alice/sandbox
USER_ID=$(id -u) GROUP_ID=$(id -g) docker compose build
alice-down --rm    # remove old container so the new image takes effect
alice-up           # start fresh on new image
```

## Debugging

- `docker logs alice` — s6 + service startup output
- `alice-shell` → poke around as Alice
- `docker inspect alice` → see mounts, env, network
- Speaking daemon log: `docker logs alice-worker-blue` (or `-green`)
- Signal daemon log: `~/.local/state/alice/signal-daemon.log`

## Extending (adding personal sidecars)

Drop a `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  alice:
    volumes:
      - ${HOME}/my-sidecars/smart-home:/home/alice/alice-tools/smart-home:rw
    environment:
      MY_API_KEY: "${MY_API_KEY}"
```

Compose merges automatically. Sidecars are accessible from within Alice's
environment without modifying the base runtime.

## Known tradeoffs

- **Network isolation is soft.** The bridge network lets Alice reach your
  LAN by default. Tighten via `networks:` if you need it.
- **GitHub token scope is whatever your PAT has.** The mounted `gh` config
  gives Alice the same scopes. Scope accordingly or use a deploy token.
- **macOS note:** `~/.ssh` mounted from macOS may have permissions that
  confuse `ssh`. The entrypoint normalizes them on start.
