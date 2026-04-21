# Quickstart

## What you need

- Linux or macOS
- Docker or Docker Desktop
- `gh` authenticated against your GitHub account
- A spare phone number for signal-cli (or your primary — Signal supports
  linking as a secondary device)

## Install

```bash
git clone https://github.com/jcronq/alice.git ~/alice
export PATH="$HOME/alice/bin:$PATH"
# add that export to your ~/.bashrc or ~/.zshrc
```

## First-run setup

```bash
alice-init
```

This interactive tool will:

1. Ask where Alice's mind should live:
   - `1` — clone an existing mind repo
   - `2` — use an existing local path
   - `3` — create a fresh one from the default scaffold *(default)*
2. Ask for your Signal account number and allowlisted senders, and write
   `~/.config/alice/alice.env`.
3. Print next-step commands.

If you picked the scaffold, `~/alice-mind` is now a local git repo. Edit
`IDENTITY.md`, `CLAUDE.md`, `USER.md` to taste.

## Register Signal

```bash
signal-cli -a "$(. ~/.config/alice/alice.env; echo "$SIGNAL_ACCOUNT")" link -n "Alice"
```

This prints a QR code. Open Signal on your phone → Settings → Linked
Devices → Link New Device → scan the QR. signal-cli's registration state
lands in `~/.local/share/signal-cli/` — that's mounted into the container.

*Can't link? You can also `register` a net-new number via signal-cli; see
signal-cli docs.*

## Start Alice

```bash
alice-up            # build image if needed, start container, wait for ready
alice -p "ping"     # one-shot test — should print the model's reply
```

From an allowlisted sender's phone, text the Signal number Alice is
registered as. Within a few seconds she should reply.

## Daily use

```bash
alice                 # interactive chat
alice -p "…"          # one-shot
alice-shell           # drop into her container (debugging)
alice-down            # stop (state preserved)
alice-down --rm       # stop and remove container (volumes persist)
```

## Versioning her mind

`~/alice-mind` is just a git repo. If you pointed it at a remote during
`alice-init`, the in-container `alice-autopush` service commits and
pushes every 15 minutes.

To configure a remote on an existing local scaffold:

```bash
cd ~/alice-mind
gh repo create --private --source . --push
```

## Updating Alice

Pull the runtime:

```bash
cd ~/alice
git pull
cd sandbox
USER_ID=$(id -u) GROUP_ID=$(id -g) docker compose build
alice-down --rm
alice-up
```

Her mind is untouched by a runtime update — she comes back with everything
she knew before.
