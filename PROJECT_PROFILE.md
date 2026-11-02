# PROJECT_PROFILE.md — jcronq/alice

Short reference for workers (background sub-agents, SM v2 lanes, humans
dropping in on a branch). The daemon reads nothing from this file —
it's a human + worker contract. Keep it terse.

## How to run tests

```
pytest -q src/
```

The suite lives under `tests/` and imports from `src/`; `pyproject.toml`
wires the import path. One-off subset:

```
pytest -q tests/test_background_task_completion.py
```

Lint / typecheck pre-commit hooks are configured in
`.pre-commit-config.yaml` — never pass `--no-verify`.

## Where the daemon lives

Alice runs as a single Docker container (`alice`) on **aimax1**
(10.20.30.177). Supervised by s6-overlay; services include speaking,
thinking, memory worker, cue runner, status site, LiteLLM gateway,
signal-cli RPC, Mosquitto, cozylobe. See `sandbox/docker-compose.yml`
and `sandbox/Dockerfile`.

Repo bind-mounts inside the container:

- Host `/home/cronqj/dev/alice/runtime/` → container `/home/alice/alice`
- Host `~/alice-mind/` → container `/home/alice/alice-mind`

State / scratch (NOT the repo):

- `/state/worker/` — writable work tree + reload sentinel + log files
- `/state/worker/speaking.log` — speaking-daemon log; grep for
  `event":"started"` after a reload to confirm it came back clean

## Reload sentinel path

Code hot-reloads via:

```
echo '{"type":"hot"}' > /state/worker/reload-requested
```

The watcher polls once per second; `event":"reload_requested"` and
then `event":"started"` land in `speaking.log` within ~10 seconds for
a successful hot reload. See `reference_speaking_worker_reload_sentinel`.

A `"type":"hot-internal"` payload is the watcher flag — do NOT write
it from a worker, you want `hot`.

## Deploy

Hot-reload is enough for pure-Python changes. For anything that touches
Dockerfile, s6 service definitions, or the `uv.lock` pin set, a full
rebuild is required:

```
# On aimax1 (NOT inside the container)
cd /home/cronqj/dev/alice/runtime
git pull
docker buildx bake -f sandbox/docker-compose.yml alice
docker compose -f sandbox/docker-compose.yml up -d alice
```

Then confirm liveness: `docker logs --tail 50 alice` and
`curl http://127.0.0.1:8080/api/v1/rpc` (should 400-with-RPC-error, not
refuse the connection).

## Ports in use

Inside the container (and published to the aimax1 host):

| Port | Service                                      |
|------|----------------------------------------------|
| 8080 | signal-cli JSON-RPC                          |
| 8081 | Zigbee2MQTT (external, must not collide)     |
| 8095 | status site (http://10.20.30.177:8095)       |
| 4000 | LiteLLM gateway                              |
| 1883 | Mosquitto (external)                         |

New services on aimax1 must avoid these — see
`reference_aimax1_port_8080_signal_cli`.

## Invariants (don't bypass)

- **Never bypass `send_message`.** Speaking's outbound to Signal / CLI
  / Discord / Gmail goes through the `mcp__alice__send_message` tool
  and the daemon's `_send_message` router. Raw `signal-cli`, `curl`
  to the RPC, or `notify` shelling breaks quiet-hours, outbox dedup,
  and surface handling. See `CLAUDE.md § Signal Messaging`.
- **Memory writes go through the memory worker.** Speaking does NOT
  write to `cortex-memory/`, `events.jsonl`, or dailies. Observations
  land in `inner/notes/` via `append_note`; the memory worker drains
  them on its own cadence.
- **Don't lean on Home Assistant.** HA is phasing out; CozyHem is the
  source of truth. See `feedback_dont_lean_on_ha_state`.
- **Thinking designs, Speaking builds.** Thinking has no WebSearch,
  no code-write surface — if a note surfaces a problem that needs
  code, Thinking writes the design and Speaking dispatches the build.
- **Workers emit a REQUIRED-REPORT.** Every background sub-agent run
  ends with a trailing fenced ```json block carrying `kind` +
  evidence (`commit_sha` / `pr_url` / suite counts for code; the
  vault path for research). See
  `src/alice_speaking/worker_report.py`.

## Off-limits

- **Never force-push** to `master` or any branch tracked by an open
  PR. Use `git push --force-with-lease` on a feature branch only if
  you absolutely must, and only if nobody else is working on it.
- **Never pass `--no-verify`** to `git commit` / `git push`. If a
  hook fails, fix the issue and create a NEW commit — do not amend
  or skip.
- **Never edit `~/.claude/.credentials.json`** or any file under
  `~/.claude/`. OAuth tokens are long-lived; the SDK rotates them on
  its own. See `feedback_use_existing_oauth_token`.
- **Never commit to Speaking's own `.claude/settings.json`** without
  understanding the hook model — hooks fire inside the daemon's
  harness, not inside your subagent. See the Stop-evidence check
  in `_dispatch.py::handle_background_task_complete` for the
  server-side equivalent.

## Where things live

- Worker-report contract — `src/alice_speaking/worker_report.py`
- Sub-agent dispatch (bg-task flow) —
  `src/alice_speaking/daemon.py::_dispatch_subagent` +
  `src/alice_speaking/_dispatch.py::handle_background_task_complete`
- SM v2 code-worker lane — `src/alice_forge/dispatcher/spawn.py`,
  rules in `src/core/agent_library/agents.py`
- Speaking's own operating manual — top-level `CLAUDE.md`
