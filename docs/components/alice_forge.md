# alice_forge

**TBD** — the idea state machine. Tracks ideas through `draft → selected → designing → designed → building → reviewing → validating → done` (with `blocked` and `rejected` escapes). Source: `src/alice_forge/`.

Filled in as PRs touch the package. Points worth covering when this stub becomes real prose:

- state diagram and allowed transitions
- artifact classification (`art:code`, `art:experiment`, `art:research_note`, `art:config_change`)
- tactical threshold rules (when an idea bypasses design)
- relationship to GitHub issues and the `sm:*` label set

## Configuring which GitHub repos Alice targets

The forge dispatcher, reconcilers, pollers, status site, viewer and the per-issue CLIs all resolve their target repos through one module, `src/core/forge_repos.py`. There is **no built-in default**: if nothing is configured, services fail at startup with `ForgeNotConfigured` naming the missing setting.

Resolution order, per value (first hit wins):

1. Environment variable, or the same key in `alice.env` (`$ALICE_CONFIG`, default `~/.config/alice/alice.env`). The resolver reads the file itself, so the key works even though compose doesn't forward it into the container.
2. The `"forge"` block of `<mind>/config/alice.config.json`.

| Value | Env var | `forge` key |
|---|---|---|
| All watched repos (comma-separated / list) | `ALICE_FORGE_REPOS` | `repos` |
| Primary repo (design/audit comments, thinking queue, status pages) | `ALICE_FORGE_REPO` | `primary` |
| Bug-fix dispatcher repo | `ALICE_BUG_FIX_REPO` | `bug_fix_repo` |

If only the repo list is set, the primary is its first entry. Entries must be `owner/name`; malformed entries are ignored.

```json
{"forge": {"repos": ["you/alice"], "primary": "you/alice", "bug_fix_repo": "you/alice"}}
```

Existing deployments that relied on the old hardcoded values should set `ALICE_FORGE_REPOS=jcronq/cozyhem-engine,jcronq/alice`, `ALICE_FORGE_REPO=jcronq/alice` and `ALICE_BUG_FIX_REPO=jcronq/modern-mud`.

Per-service overrides that predate this (`GH_RECONCILER_REPOS`, `ALICE_SM_REPO`, the dispatcher/watcher `repos` blocks in `alice.config.json`) still take precedence for their own service.
