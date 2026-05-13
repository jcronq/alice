# host-claude-watcher

Host-side daemon that bridges **Speaking** (running inside the
`alice-worker` container, which has no host filesystem access and no
`claude` binary) to a **Claude CLI** running on the host.

## How it works

1. Speaking calls the MCP tool `request_host_claude(prompt=...)`. The
   tool writes a markdown file with YAML frontmatter to
   `/state/worker/host-claude/inbox/<UTC-ISO-ts>-<slug>.md`. Atomic
   tempfile+rename means the daemon never sees a half-written file.
2. This daemon (running on the host as `alice-host-claude.service`)
   watches `inbox/` with `inotifywait`. For every new `*.md`:
   - It parses the frontmatter (`id`, `timeout_seconds`, ...).
   - Runs `claude --print --max-turns 50 < <body>` under
     `timeout(1)` with the requested wall-clock cap.
   - Writes the captured stdout + stderr to
     `/state/worker/host-claude/outbox/<same-id>.md`, atomically.
   - Moves the inbox file into `.handled/<YYYY-MM-DD>/`.
3. Speaking's tool polls `outbox/<id>.md`. When it appears, the tool
   parses the frontmatter and returns `status`, `stdout`, `stderr`,
   `exit_code` to the agent.

Output is truncated to 50 KB per stream with a trailing marker — Signal-
sized agents don't want a 200 KB log in their context window anyway.

## Layout (on the host)

```
/state/worker/host-claude/
├── inbox/                    # tasks waiting to run
├── outbox/                   # finished tasks (consumed by Speaking)
└── .handled/                 # dated archive of processed inbox files
    └── 2026-05-12/
        └── 2026-05-12T13-42-08Z-foo.md
```

The directory is bind-mounted into the worker container at the same
path, so `inbox/` and `outbox/` mean the same thing on both sides.

## Install

The systemd unit runs `alice-host-claude-watcher.sh` directly from the
bind-mounted repo (`ExecStart=${ALICE_REPO}/sandbox/host-claude-watcher/...`)
so updating the daemon code is just `git pull && systemctl restart
alice-host-claude` — no `/usr/local/bin/` copy step. Issue #144 traced a
multi-hour stall to that copy step being skipped after PR #138 merged.

```bash
# 1. Drop the systemd unit. Edit User= / Group= if you're not running
#    as the `alice` host user; the binary needs to be on that user's
#    PATH and ~/.claude needs to be readable. Edit ALICE_REPO if your
#    clone lives somewhere other than ~/alice.
sudo cp alice-host-claude.service /etc/systemd/system/

# 2. Create the shared bind-mount dir if it doesn't exist yet.
sudo install -d -o alice -g alice /state/worker/host-claude/{inbox,outbox,.handled}

# 3. Enable + start.
sudo systemctl daemon-reload
sudo systemctl enable --now alice-host-claude.service
sudo systemctl status alice-host-claude.service
```

## Update

After `git pull`ing a change to `alice-host-claude-watcher.sh` or to the
unit file:

```bash
# If the unit file changed:
sudo cp alice-host-claude.service /etc/systemd/system/ && sudo systemctl daemon-reload
# Always:
sudo systemctl restart alice-host-claude.service
```

Verify the daemon is running the new code by reading the startup banner:

```bash
journalctl -u alice-host-claude.service -n 20 | grep 'script_sha='
```

The `script_sha=` prefix is the first 12 chars of the script's sha256.
After a code change it must differ from the previous restart; if it
doesn't, the wrong file is being executed (e.g. an old
`/usr/local/bin/alice-host-claude-watcher.sh` shadow).

The daemon logs to the journal:

```bash
journalctl -u alice-host-claude.service -f
```

## Sanity-check without systemd

`alice-host-claude-watcher.sh --single-shot` drains the inbox once and
exits, skipping the watch loop. That's also what
`test-roundtrip.sh` uses to round-trip a synthetic request — see the
script for the full shape of an inbox/outbox file.

```bash
./test-roundtrip.sh
```

## Notes

- **Doesn't run inside the worker container.** The worker has no host
  PATH, no `claude` binary, and the wrong working directory. Ship this
  on the host, period.
- **Doesn't run as root.** `claude` would write into root's home, which
  is not what you want.
- **Refuses to start if `claude` isn't on PATH.** Set
  `ALICE_HOST_CLAUDE_BIN` to override.
- **Crash recovery is implicit.** Anything in `inbox/` is drained on
  startup before the watch loop begins, so a daemon restart never
  loses a queued request.
