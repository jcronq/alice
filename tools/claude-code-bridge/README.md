# Claude Code bridge → Alice

Lets Claude Code sessions on a dev host (Bazzite, 10.20.30.199) talk to Alice
without giving that host a shell on aimax1:

- **Surfaces.** A Claude Code `Stop` hook (`alice-cc-hook.py`) drops a
  `claude-code-turn-done` surface into Alice's `inner/surface/` inbox when a turn
  ran ≥10 min or a background workflow finished. `--progress` / `--watch` modes
  drop `claude-code-progress` surfaces (batched commits, stalls).
- **Status pages.** Generators such as `mud-wave-status.py` render a markdown page
  and publish it to the [status site](../../docs/designs/status-site.md) as
  `<name>.md`.

Both go over ssh with **dedicated, restricted keys**: the key can only run a
forced command on aimax1 that reads stdin and writes one file. No shell, no
forwarding, and only from the dev host's IP.

```
dev-host/alice-cc-hook.py     ─ssh alice_surface─▶  aimax1:~/bin/alice-surface-drop ─▶ mind/inner/surface/<ts>-cc-<detail>.md
dev-host/<generator>.py       ─ssh alice_status <page>─▶ aimax1:~/bin/alice-status-drop ─▶ mind/inner/status/<page>.md
dev-host/<generator>.py       ─ssh alice_status extra/<slug>─▶ alice-status-drop ─▶ mind/inner/status/item-extra/<slug>.jsonl
```

| File | Lives on | Purpose |
|------|----------|---------|
| `aimax1/alice-surface-drop` | aimax1 `~/bin/` | Forced command. stdin (≤64 KB, must start with frontmatter) → surface file. Ignores `SSH_ORIGINAL_COMMAND`. |
| `aimax1/alice-status-drop` | aimax1 `~/bin/` | Forced command. stdin (≤256 KB) → `<page>.md`; page name from `SSH_ORIGINAL_COMMAND`, `[a-z0-9-]{1,40}`, never `index`. Command `extra/<slug>` instead writes JSON Lines (≤64 KB, ≤500 objects) to `item-extra/<slug>.jsonl`, shown in that item's "what happened" timeline. |
| `dev-host/alice-cc-hook.py` | dev host `~/bin/` | Stop hook + `--progress` / `--watch` modes. |
| `dev-host/mud-wave-status.py` | dev host `~/bin/` | Example status generator (modern-mud wave-A build). |
| `systemd/alice-status-page@.{service,timer}` | dev host `~/.config/systemd/user/` | Runs a generator every 3 min. |

Both receivers find the mind dir from `$ALICE_MIND`, else `ALICE_MIND=` in
`~/.config/alice/alice.env`, else `~/alice-mind`.

## Setup

### 1. Keys (on the dev host)

One key per receiver, no passphrase (hooks run unattended). **Never commit
them.**

```bash
ssh-keygen -t ed25519 -N '' -C "$(hostname)-cc-hook->alice-surface" -f ~/.ssh/alice_surface
ssh-keygen -t ed25519 -N '' -C "$(hostname)->alice-status"          -f ~/.ssh/alice_status
```

### 2. Receivers + authorized_keys (on aimax1)

```bash
install -m 755 tools/claude-code-bridge/aimax1/alice-surface-drop ~/bin/
install -m 755 tools/claude-code-bridge/aimax1/alice-status-drop  ~/bin/
```

Append to `~/.ssh/authorized_keys`, one line each, pasting the `.pub`
contents. `from=` pins the key to the dev host's address:

```
command="/home/cronqj/bin/alice-surface-drop",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,from="10.20.30.199" ssh-ed25519 AAAA... bazzite-cc-hook->alice-surface
command="/home/cronqj/bin/alice-status-drop",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding,from="10.20.30.199" ssh-ed25519 AAAA... bazzite->alice-status
```

(`restrict,command="..."` is the modern equivalent of the `no-*` list.)

Check from the dev host. Each should print a filename or a `rejected:` line, never a shell:

```bash
printf -- '---\ndetail: smoke\n---\ntest\n' | ssh -i ~/.ssh/alice_surface -o IdentitiesOnly=yes cronqj@10.20.30.177
echo '# smoke' | ssh -i ~/.ssh/alice_status -o IdentitiesOnly=yes cronqj@10.20.30.177 smoke-test
```

Then delete the smoke surface and `smoke-test.md` from the mind dir.

### 3. Stop hook (on the dev host)

```bash
install -m 755 tools/claude-code-bridge/dev-host/alice-cc-hook.py ~/bin/
```

`~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "/home/cronqj/bin/alice-cc-hook.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

The hook forks and returns immediately, so it never slows Claude Code down.
It de-dupes in `~/.cache/alice-cc-hook/seen.json`. Optional env:
`ALICE_SSH` (default `cronqj@10.20.30.177`), `ALICE_SURFACE_KEY` (default
`~/.ssh/alice_surface`).

Long builds: run the progress watcher next to the session (it exits when the
PID dies):

```bash
nohup ~/bin/alice-cc-hook.py --progress ~/dev/REPO BRANCH ~/.claude/projects/SESSION_DIR "$CLAUDE_PID" >/dev/null 2>&1 &
```

### 4. Status page generators (systemd user timer, dev host)

```bash
install -m 755 tools/claude-code-bridge/dev-host/mud-wave-status.py ~/bin/
cp tools/claude-code-bridge/systemd/alice-status-page@.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alice-status-page@mud-wave-status.timer
loginctl enable-linger "$USER"   # keep timers running when logged out
```

The instance name is the script's basename in `~/bin` (without `.py`). Add the
page to `inner/status/pages.json` once, e.g.
`{"slug":"wave-a","title":"Howl Wave A"}`, so it shows in the nav.

(Bazzite still runs the pre-template units `mud-wave-status.{service,timer}`,
which do the same thing.)
