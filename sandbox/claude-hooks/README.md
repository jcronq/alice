# sandbox/claude-hooks

User-global Claude Code hook scripts shipped alongside the alice repo. These
install into `~/.claude/hooks/` on each machine that runs the harness; the
canonical runtime copy lives at `/home/alice/.claude/hooks/` inside the alice
container and at `/home/cronqj/.claude/hooks/` on the aimax1 host.

Design reference: `cortex-memory/designs/2026-11-24-subagent-driven-running-now.md`.

| Script                     | Claude Code event | What it does                                                                                          |
|----------------------------|-------------------|-------------------------------------------------------------------------------------------------------|
| `subagentstop-emit.py`     | `SubagentStop`    | Append a clean stop row to `/state/worker/alice-subagent-stop.jsonl` whenever an `Agent` subagent exits. |
| `stop-session-reaper.py`   | `Stop`            | On session end, synthesise `subagent_reaped{reaped_reason=session_end}` for orphans in that session.    |

Both hooks share `/state/worker/alice-subagent-stop.jsonl` with
`src/hooks/orphan_reaper.py` (the periodic + boot-time reaper). All three
write under `fcntl.flock` so concurrent appends are safe.

## Install

### Local workstation

```bash
cp sandbox/claude-hooks/*.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/subagentstop-emit.py ~/.claude/hooks/stop-session-reaper.py
```

Then register in `~/.claude/settings.json` (preserving the existing
`PreToolUse Agent` block):

```json
"SubagentStop": [
  { "hooks": [
      { "type": "command",
        "command": "python3 /home/alice/.claude/hooks/subagentstop-emit.py"
      }
  ] }
],
"Stop": [
  { "hooks": [
      { "type": "command",
        "command": "python3 /home/alice/.claude/hooks/stop-session-reaper.py"
      }
  ] }
]
```

### aimax1 alice container

Hooks run inside the container's `~/.claude/` namespace, so copy them in
with `docker cp` (they persist until the container is recreated — see
`reference_cozyhem_deploy_and_config_layout` and
`reference_docker_exec_root_via_host`):

```bash
ssh aimax1 '
  docker cp /home/cronqj/dev/alice/runtime/sandbox/claude-hooks/subagentstop-emit.py   alice:/home/alice/.claude/hooks/
  docker cp /home/cronqj/dev/alice/runtime/sandbox/claude-hooks/stop-session-reaper.py alice:/home/alice/.claude/hooks/
  docker exec alice chmod +x /home/alice/.claude/hooks/subagentstop-emit.py /home/alice/.claude/hooks/stop-session-reaper.py
'
```

The `settings.json` edit is identical to the local install; pipe it through
`docker exec -i alice tee /home/alice/.claude/settings.json` after validating
locally. For a baked-in deploy, add the copy to `sandbox/Dockerfile`.
