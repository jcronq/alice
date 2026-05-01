# PiKernel — Codex/ChatGPT subscription routing for thinking

Operator guide for routing the thinking hemisphere through
pi-coding-agent (Mario Zechner's Node CLI) so wakes spend the
ChatGPT subscription quota instead of the Anthropic monthly cap.
Designed for the moment Sonnet/Opus limits hit and thinking still
needs to keep producing notes/dailies/grooming output.

The runtime side ships in five phases (A–E) of the plan-pi rollout.
Operator action lands in this doc.

## Prerequisites

1. **ChatGPT Plus or Pro subscription** with Codex access included
   (Plus $20/mo or Pro $200/mo, per
   <https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan>).
2. **Codex CLI installed on the host** (`npm i -g @openai/codex`
   or whatever the install command is). The host runs the device-
   auth flow once; the resulting auth file is bind-mounted into the
   worker container.
3. **Working host networking** for the OAuth device flow at first
   sign-in. After tokens are cached, refreshes happen against
   `https://auth.openai.com` from inside the worker.

## Step 1 — Authenticate on the host

```bash
codex login                      # device-auth: opens auth.openai.com URL
                                 # paste code, sign in, granted
ls ~/.codex/auth.json            # confirm it landed
jq -r '.auth_mode' ~/.codex/auth.json   # → "chatgpt"
```

If you see `auth_mode: "apikey"`, you've authenticated against the
OpenAI Platform (API-key billing) instead of the ChatGPT plan.
That's fine for development but it bills against
`platform.openai.com` not your ChatGPT subscription. Re-run
`codex logout && codex login` and select the ChatGPT path when
prompted.

## Step 2 — Wire model.yml

Edit `mind/config/model.yml`:

```yaml
speaking:
  backend: subscription           # Anthropic Max — keep speaking on it
  model: claude-opus-4-7
thinking:
  backend: pi                     # codex via pi-coding-agent
  model: gpt-5.3-codex            # PiKernel adds the openai-codex/ prefix
viewer:
  backend: subscription
  model: claude-haiku-4-5-20251001
```

Available pi/Codex models (run `pi --list-models` on the host or
inside the worker after deploy):

- `gpt-5.1` — general-purpose, 272K context
- `gpt-5.1-codex-max` — coding-tuned, larger context handling
- `gpt-5.1-codex-mini` — cheap, fast
- `gpt-5.2`, `gpt-5.2-codex` — recent generation
- **`gpt-5.3-codex`** — recommended default for thinking
- `gpt-5.3-codex-spark` — even cheaper, but the spike found
  pi's harness occasionally misbehaves with it
- `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.5` — frontier; check ChatGPT
  plan limits before defaulting here

Power users can write `model: openai-codex/gpt-5.3-codex`
explicitly. Same routing.

## Step 3 — Deploy

```bash
bin/alice-deploy worker          # pulls Dockerfile changes:
                                 #   npm i -g @mariozechner/pi-coding-agent@0.71.0
                                 #   compose mounts ~/.codex:/host-codex:ro
                                 # entrypoint runs codex-to-pi-auth at startup,
                                 # producing ~/.pi/agent/auth.json inside the worker.
```

Watch the entrypoint log on first deploy:

```bash
docker logs $(docker ps --format '{{.Names}}' | grep alice-worker | head -1) 2>&1 | head -20
# expect a line like:
#   [entrypoint] pi auth bridged from /host-codex/auth.json
#   wrote /home/alice/.pi/agent/auth.json (openai-codex; expires in ~14400 min)
```

## Step 4 — Verify

```bash
bin/alice-backend show
# speaking: backend=subscription  model=claude-opus-4-7
# thinking: backend=pi  model=gpt-5.3-codex
# viewer:   backend=subscription  model=claude-haiku-4-5-20251001
#
#       pi: openai-codex token valid (~14400 min remaining)

bin/alice exec pi --list-models | head -10
# openai-codex  gpt-5.1                272K  128K  yes  yes
# openai-codex  gpt-5.3-codex          272K  128K  yes  yes
# ...
```

## Step 5 — Smoke

```bash
bin/alice-think --quick           # tiny prompt; verifies auth + JSONL plumbing
grep '"provider":"openai-codex"' /state/worker/thinking.log | tail -3
grep '"model":"gpt-5.3-codex"' /state/worker/thinking.log | tail -3
```

For a full thinking wake (writes to vault):

```bash
bin/alice-think
ls -t data/alice-mind/inner/thoughts/$(date -I)/ | head
# wake's note-file should appear here
```

## Step 6 (optional) — Ad-hoc backend override

When you've changed the host's Codex auth and want to verify the
container picks it up without editing model.yml:

```bash
bin/alice-think --backend=pi --quick
```

`--backend=` accepts `subscription | api | bedrock | pi`. The chosen
backend overrides whatever `model.yml` says for thinking, just for
that wake.

## Token rotation cadence

ChatGPT OAuth access tokens have a 10-day exp claim. Pi will try
to refresh against `https://auth.openai.com/oauth/token` when it
sees an expired token, using its hardcoded
`CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"`.

**Untested edge case:** if the Codex CLI's client_id differs from
pi's, the refresh request returns `invalid_client` and pi falls
back to "no auth available". You'll see this as `pi exited 1`
errors with `No API key found for the selected model`.

**Recovery:**

```bash
codex login                      # re-auth on the host
docker restart $(docker ps --format '{{.Names}}' | grep alice-worker | head -1)
                                 # entrypoint re-runs the bridge
bin/alice-backend show           # confirm new expiry
```

If you're running pi against a long-lived backend (a week+) it's
worth noting the expiry date returned by `alice-backend show` and
re-bridging proactively.

## Architectural notes

- **MCP gap.** Pi has no built-in Model Context Protocol client.
  Skills that need MCP-backed tools (cortex-memory's vault grep,
  e.g.) won't have those tools available when thinking runs on pi.
  Pi's built-in tools (Read, Bash, Edit, Write, Grep, Find, Ls)
  cover most thinking workflows; the gap surfaces only for skills
  that explicitly call MCP tools.

- **Subscription cost.** Pi reports a USD figure in `cost.total`
  based on API rates. **Ignore it.** Subscription billing is opaque
  — your ChatGPT plan tracks Codex usage separately (see
  `chatgpt.com/codex/usage` or whatever the plan dashboard URL is).
  The viewer renders pi-routed turns with `cost: subscription-billed`
  to avoid confusion.

- **Compaction.** Alice owns context compaction. Pi's own
  compaction is disabled at the `~/.pi/agent/settings.json` level
  so the two don't fight.

- **Sessions.** PiKernel passes `--no-session` so each wake is
  one-shot from pi's perspective; Alice's session machinery (which
  doesn't apply to thinking anyway) stays uncontested.

- **Skills.** PiKernel passes `--skill <state_dir>/alice-skills/
  thinking/.claude/skills` (the per-hemisphere ephemeral dir from
  Plan 07 P3) and `--no-skills` so directory-based discovery
  doesn't double-up. Pi's strict YAML parser is satisfied because
  the render step in Phase C re-emits frontmatter as
  double-quoted scalars.

## Troubleshooting

### `pi exited 1: No API key found for the selected model`

Pi can't read `~/.pi/agent/auth.json`. Either:

- The bridge hasn't run (check `docker logs`).
- The Codex auth file is missing on the host (re-run `codex login`).
- The expires field has passed and pi's refresh failed (re-bridge).

### `pi exited 2: Codex error: ChatGPT rate limit reached`

You've hit the ChatGPT plan's Codex usage cap. Options:

- Wait for the reset (Plus: weekly windows; Pro: more generous).
- Temporarily `bin/alice-think --backend=subscription` if you have
  Anthropic budget left.
- Upgrade the plan tier.

### Thinking output is gibberish / hangs

Check the model is one pi recognizes — `pi --list-models` post-
auth. If `model.yml` has e.g. `model: claude-sonnet-4-6`, PiKernel
prepends `openai-codex/` and pi rejects the unknown model. Switch
to a pi-recognized name (`gpt-5.3-codex` etc.).

### `[entrypoint] WARNING: codex-to-pi-auth failed`

The bridge ran but couldn't read the codex auth or wrote nothing.
Check the script's stderr in `docker logs` — it names the failing
file or the missing JWT claim. Most common: `~/.codex/` mount
isn't visible (compose volume drift), or `auth_mode: apikey`
instead of `chatgpt`.

## Cleanup if abandoning pi

```bash
# In model.yml, switch thinking back to subscription (or whatever
# Anthropic-shaped backend you were using):
sed -i 's/backend: pi/backend: subscription/' \
    data/alice-mind/config/model.yml
bin/alice-deploy worker
```

The pi binary stays installed in the image (it's <100MB and
useful for ad-hoc `--backend=pi` smoke tests later); the auth file
in `~/.pi/agent/auth.json` is ignored when no hemisphere is on
backend: pi.
