# Spike report — pi-coding-agent as a Codex-fallback kernel candidate

**Date:** 2026-05-01
**Versions tested:** pi-coding-agent 0.71.0, node 24.12.0
**Author:** runtime spike, signed off by Jason
**Status:** docs verified + live end-to-end run completed via auth-file bridge from Codex CLI

## Headline finding

Pi's `/login` flow uses a local OAuth callback server (binds
`127.0.0.1:1455` for the redirect) and has no `--device-auth`
mode — so it doesn't work on a headless host without browser
forwarding. **Workaround that works today**: run `codex login`
on the host (which DOES have a device-auth flow), then translate
`~/.codex/auth.json` into `~/.pi/agent/auth.json`'s shape. Pi
accepts the bridged token and runs Codex models end-to-end. The
translation script is small (~20 lines of Node.js) and lives
inline in this report below.

The 10-day token window is comfortably within an Alice deploy
cycle. The one untested edge case is what happens at expiry —
pi will attempt to refresh using its hardcoded
`CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"`. If Codex CLI uses
a different client_id, pi's refresh will fail and the operator
re-runs `codex login` + bridge. This is a known operational cost,
not a blocker.

## Goal

Decide whether to wrap `pi --mode json` as a `PiKernel` for the
thinking-side fallback path, or fall back to the narrower
`codex exec --json` wrapper. Three questions had to land before
committing to any plan:

1. **Auth.** Does pi actually expose ChatGPT-subscription OAuth at
   the CLI surface? (The lower-level `pi-ai` library has it; the
   coding-agent docs were silent in initial reads.)
2. **Skills.** Does pi consume Alice's existing
   `data/alice-mind/.claude/skills/` SKILL.md files without
   translation?
3. **Event stream.** Is `--mode json` JSONL parseable enough to
   wrap as a kernel transport?

## Verdict

**Proceed to wrap pi-coding-agent as `PiKernel`** — with one
architectural caveat (pi has no built-in MCP, see below). All three
core questions land "yes" with strong supporting evidence from the
bundled docs and observed startup behavior.

If the live OAuth verification fails for any reason, the fallback
is the narrower `CodexKernel` wrapping `codex exec --json`
directly. The architecture (Kernel Protocol + per-hemisphere
backend selection) is identical between the two; only the
subprocess target changes.

## Findings

### Phase 1 — Auth: ✅ first-class subscription OAuth (with headless workaround)

**Live result:** pi → Codex via bridged auth runs end-to-end.
`pi --provider openai-codex --model gpt-5.3-codex --mode json -p
"reply with exactly: ok"` produced `"ok"` in 3 seconds, 1055
tokens total. Hits `https://chatgpt.com/backend-api/codex/responses`
(subscription endpoint), so billing goes against the ChatGPT
subscription quota.

**The headless gotcha.** Pi's interactive `/login` opens a local
HTTP server on `127.0.0.1:1455` to catch the OAuth redirect,
which is useless on a headless host with no browser. There's no
`--device-auth` flag. Codex CLI has device-auth (works fine
headless), so the practical path is **bridge from `codex login`'s
auth file to pi's auth file**.

**Auth file shapes:**

```text
~/.codex/auth.json (Codex CLI):
{
  "auth_mode": "chatgpt",
  "tokens": {
    "id_token": "eyJhbGci…",
    "access_token": "eyJhbGci…",   // JWT bearer
    "refresh_token": "rt_…",
    "account_id": "<uuid>"
  },
  "last_refresh": "<ISO>"
}

~/.pi/agent/auth.json (pi):
{
  "openai-codex": {
    "type": "oauth",
    "access": "<JWT>",                // = codex.tokens.access_token
    "refresh": "<token>",             // = codex.tokens.refresh_token
    "expires": <unix-ms>,             // from JWT exp claim * 1000
    "accountId": "<uuid>"             // from JWT chatgpt_account_id claim
  }
}
```

**Translation script (proven working):**

```javascript
// /home/cronqj/alice/bin/codex-to-pi-auth (or similar)
const fs = require('fs');
const codex = JSON.parse(fs.readFileSync(process.env.HOME + '/.codex/auth.json', 'utf8'));
const access = codex.tokens.access_token;
const [, payloadB64] = access.split('.');
const padded = payloadB64 + '='.repeat((4 - payloadB64.length % 4) % 4);
const payload = JSON.parse(Buffer.from(padded, 'base64').toString());
const piAuth = {
  'openai-codex': {
    type: 'oauth',
    access,
    refresh: codex.tokens.refresh_token,
    expires: payload.exp * 1000,
    accountId: payload['https://api.openai.com/auth'].chatgpt_account_id,
  },
};
fs.writeFileSync(process.env.HOME + '/.pi/agent/auth.json', JSON.stringify(piAuth, null, 2));
fs.chmodSync(process.env.HOME + '/.pi/agent/auth.json', 0o600);
```

**Token expiry / refresh.** JWT exp is 10 days from issuance.
When pi sees `Date.now() >= cred.expires` it calls
`https://auth.openai.com/oauth/token` with grant_type=refresh_token
+ pi's hardcoded CLIENT_ID. **Untested:** whether pi's CLIENT_ID
(`app_EMoamEEZ73f0CkXaXp7hrann`) matches Codex CLI's. If they
differ, pi's refresh will return invalid_client and the operator
must re-run `codex login` + bridge. Worst case: the spike
documented a 10-day rotation cadence. Best case: client_ids match
and refresh just works.

**For container deployment:** bind-mount `~/.codex/` into the
worker (read-only), run the bridge script at worker startup
(writes `~/.pi/agent/auth.json` inside the container — needs to
be writable). On `codex` re-auth on the host, the next worker
restart picks up the fresh token.

**Bonus:** pi also supports Claude Pro/Max OAuth via the same
`/login` flow — but it has the same headless limitation, and
unlike Codex there's no Anthropic-side device-auth path. So the
Anthropic-Max-via-pi route stays out of scope unless we can run
the OAuth flow on a host with a browser then bridge.

From `providers.md` line 14-22 (bundled with the package, verbatim):

> ## Subscriptions
> Use `/login` in interactive mode, then select a provider:
> - Claude Pro/Max
> - **ChatGPT Plus/Pro (Codex)**
> - GitHub Copilot
>
> Use `/logout` to clear credentials. Tokens are stored in
> `~/.pi/agent/auth.json` and auto-refresh when expired.

And the dedicated Codex subsection (`providers.md` line 34-37):

> ### OpenAI Codex
> - Requires ChatGPT Plus or Pro subscription
> - Personal use only; for production, use the OpenAI Platform API

Behaviorally confirmed: running `pi --mode json -p "ping"` without
auth produces a clean session-header event then errors out
pointing at `/login`:

```
{"type":"session","version":3,"id":"...","timestamp":"...","cwd":"/tmp"}
No API key found for the selected model.
Use /login to log into a provider via OAuth or API key.
```

This is the failure mode we want — visible, actionable, no silent
degradation.

**Bonus:** Pi also supports Claude Pro/Max OAuth via the same
`/login` flow. Could substitute for `claude_agent_sdk`'s
subscription path entirely if we ever want one wrapper for both
hemispheres. Not in scope for this spike.

### Phase 2 — Skills: ✅ load + match correctly, with two rendering caveats

**Live result:** pi loaded 3 of 4 SKILL.md files from
`data/alice-mind/.claude/skills/` directly. After applying the
fix described below, the rendered `log-meal` loaded cleanly and
pi's model correctly identified it as the right skill for a
meal-shaped prompt:

> User: "I ate chicken and rice for lunch. ... tell me which of
> your skills you would use."
>
> Pi (gpt-5.3-codex): "If you wanted me to log it, I'd use the
> **`log-meal`** skill (`/tmp/pi-spike-skills/log-meal/SKILL.md`)."

**Caveat A — pi's YAML is strict; Alice's `log-meal` description
isn't.** Pi rejected `log-meal` with:

```
Nested mappings are not allowed in compact mappings at line 2, column 14:
description: Use when {{ user.name }} reports eating something ("I ate X", "bre…
```

The description contains `"lunch: X"` — strict YAML parses the
inner colon as starting a nested mapping. Claude Code's loader
is lenient enough to accept it (Alice's own
`_parse_frontmatter_lenient` in
`src/alice_skills/skill.py` handles this); pi's isn't. The other
three Alice skills happen to pass because their inline examples
don't contain colons.

**Fix:** the Plan 07 P3 render step has to do TWO things, not
one: (1) substitute `{{ user.name }}` / `{{ agent.name }}` Jinja
(already planned), and (2) re-emit frontmatter as
strict-YAML-compliant — double-quoted scalars escape the inline
colons. Proven working with pi's bundled `yaml` package and
`{ defaultStringType: 'QUOTE_DOUBLE' }`.

**Caveat B — README.md is treated as a candidate skill.** Pi's
discovery rules: "In `~/.pi/agent/skills/` and `.pi/skills/`,
direct root .md files are discovered as individual skills." Pi
applied that rule to `data/alice-mind/.claude/skills/README.md`
and reported "description is required" because the README has no
frontmatter. Two fixes: (a) move the README to a sibling
location outside the skills dir, or (b) the render step writes
only the rendered SKILL.md tree to `<state_dir>/alice-skills/<hemi>/`
and the README never gets there.

**Implication for Plan 07 Phase 3.** What was originally drafted
as "render SKILL.md with Jinja substitution" now has three jobs:

1. Filter by hemisphere scope (already in plan).
2. Substitute Jinja placeholders (already in plan).
3. **NEW:** Re-emit frontmatter as strict-YAML-compliant via
   `yaml.stringify(fields, { defaultStringType: 'QUOTE_DOUBLE' })`
   or the Python equivalent.

The render step writes to a per-hemisphere ephemeral dir (the
plan's existing target location). README.md and other non-skill
files don't get copied. Pi's auto-discovery sees only the
rendered tree.

From `skills.md` line 43-54 (verbatim):

> ### Using Skills from Other Harnesses
> To use skills from Claude Code or OpenAI Codex, add their
> directories to settings:
>
> ```json
> {
>   "skills": [
>     "~/.claude/skills",
>     "~/.codex/skills"
>   ]
> }
> ```

Pi implements the [Agent Skills standard](https://agentskills.io/specification)
the same way Claude Code does. Required frontmatter keys
(`name`, `description`) match Alice's. Optional pi-specific keys
(`license`, `compatibility`, `metadata`, `allowed-tools`,
`disable-model-invocation`) are additive. Pi explicitly ignores
unknown frontmatter (`skills.md` line 185), so Alice's `scope:`
field is harmless.

**Configured `~/.pi/agent/settings.json`** to point at Alice's
skill dir:

```json
{
  "skills": [
    "/home/cronqj/alice/data/alice-mind/.claude/skills"
  ]
}
```

Inventory of what pi will see:

```
data/alice-mind/.claude/skills/
├── README.md                  # ignored — pi requires SKILL.md
├── log-meal/SKILL.md          # name=log-meal, scope=speaking
├── log-workout/SKILL.md       # name=log-workout, scope=speaking
├── update-weight/SKILL.md     # name=update-weight, scope=speaking
└── cortex-memory/SKILL.md     # name=cortex-memory, scope=thinking
```

**Caveat — descriptions use Jinja templating.** Alice's skill
descriptions contain `{{ user.name }}` and `{{ agent.name }}`
placeholders that the runtime renders against `personae.yml`
before the SDK auto-loader sees them. Pi reads SKILL.md raw and
will show the model literal `{{ user.name }}` text.

This was always going to be solved by Plan 07 Phase 3 (render
filtered skills to a per-hemisphere ephemeral dir before kernel
init). PiKernel would consume that already-rendered output. No
new work specific to pi.

**Name-rule sanity check:** all four skills are lowercase /
hyphen-separated and match their parent directory names — pi's
strict-name validator will pass them.

### Phase 3 — Event stream: ✅ clean 1:1 mapping (live-confirmed)

**Live result:** captured a full `gpt-5.3-codex` turn under
`pi --mode json -p "reply with exactly: ok"`. Event order
matched the documented vocabulary exactly. Sample (one event per
line in real output):

```
{"type":"session","version":3,"id":"019de135-…","cwd":"/tmp"}
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"message_start","message":{"role":"user","content":[…]}}
{"type":"message_end","message":{"role":"user",…}}
{"type":"message_start","message":{"role":"assistant","content":[],"api":"openai-codex-responses","provider":"openai-codex","model":"gpt-5.3-codex",…}}
{"type":"message_update","assistantMessageEvent":{"type":"text_start","contentIndex":0,"partial":{…}}}
{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"ok",…}}
{"type":"message_update","assistantMessageEvent":{"type":"text_end","content":"ok","partial":{…,"textSignature":"…"}}}
{"type":"message_end","message":{…,"usage":{"input":1050,"output":5,"totalTokens":1055,"cost":{…,"total":0.0019075}}}}
{"type":"turn_end","message":{…},"toolResults":[]}
{"type":"agent_end","messages":[…]}
```

**Cost field gotcha.** The `usage.cost.total` field shows USD
based on token rates (`$0.0019` for the test turn). For
subscription-billed traffic it's **informational only** — the
actual quota cost is against the ChatGPT plan's Codex usage
counter, not USD. The PiKernel wrapper should drop the cost field
or rename it to avoid implying API-rate billing.

From bundled `json.md` line 27-43 — `AgentEvent` types:

```typescript
type AgentEvent =
  | { type: "agent_start" }
  | { type: "agent_end"; messages: AgentMessage[] }
  | { type: "turn_start" }
  | { type: "turn_end"; message: AgentMessage; toolResults: ToolResultMessage[] }
  | { type: "message_start"; message: AgentMessage }
  | { type: "message_update"; message: AgentMessage; assistantMessageEvent: AssistantMessageEvent }
  | { type: "message_end"; message: AgentMessage }
  | { type: "tool_execution_start"; toolCallId: string; toolName: string; args: any }
  | { type: "tool_execution_update"; toolCallId: string; toolName: string; args: any; partialResult: any }
  | { type: "tool_execution_end"; toolCallId: string; toolName: string; result: any; isError: boolean };
```

Mapping to Alice's existing kernel events (`alice_core/kernel.py`):

| Alice event | Pi source | Notes |
|---|---|---|
| `wake_start` | `session` (first line) + `agent_start` | session has `cwd`, `id`, `timestamp` |
| `text` (accumulated) | `message_update` filtered to `assistantMessageEvent.type == "text_delta"` | concatenate `delta` fields |
| `thinking` | `message_update` filtered to `assistantMessageEvent.type == "thinking_delta"` | confirm shape during live run |
| `tool_use` | `tool_execution_start` | `toolName`, `args`, `toolCallId` |
| `tool_result` | `tool_execution_end` | `result`, `isError` |
| `result_meta` | `turn_end` | extract usage from `message.usage` |
| `wake_end` | `agent_end` | terminal event |

Bonus events pi emits that Alice doesn't currently track but could
opportunistically wire up:

- `compaction_start` / `compaction_end` — pi has built-in
  compaction; Alice has its own `SessionCompaction` handler.
  We'd want to **disable pi's** via `compaction.enabled: false`
  in `~/.pi/agent/settings.json` and let Alice's compaction logic
  remain authoritative. Otherwise both run and the result is
  unpredictable.
- `auto_retry_start` / `auto_retry_end` — pi has built-in
  exponential-backoff retry on transient errors. Alice doesn't
  have this today. Potentially valuable, even — pi's retry handles
  rate-limit-style transients without `PiKernel` having to
  duplicate the logic.
- `queue_update` — irrelevant for thinking (no steering queue).

### Phase 4 — Bonus checks

#### MCP — ⚠️ NOT BUILT-IN

From `usage.md` line 275:

> "It intentionally does not include built-in MCP, sub-agents,
> permission popups, plan mode, to-dos, or background bash. You
> can build or install those workflows as extensions or packages,
> or use external tools such as containers and tmux."

This is the most architecturally important caveat. Pi has rich
extension support (TypeScript), and an extension *could* implement
an MCP client, but no first-party MCP client ships with the
binary.

**Impact assessment for thinking-on-pi specifically:**

- Thinking's primary tools are vault Read/Write, shell `Bash`
  (run `gh`, `grep`, etc.), web fetches, and skill execution.
- Pi has built-in `read`, `bash`, `edit`, `write`, `grep`, `find`,
  `ls` tools — covers all the above.
- Alice's MCP servers (whatever the user has wired in
  `~/alice-mind/.claude/mcp.json` or equivalent) would not be
  available to thinking-on-pi without writing a TypeScript
  extension that bridges the protocol.
- For the immediate goal (Codex fallback during Anthropic
  rate-limit), MCP-less thinking is probably acceptable —
  thinking groomings/dailies/notes use the built-in tools.

If MCP turns out to be a hard blocker after live testing, the
options are: (a) write a one-shot pi extension that mounts Alice's
MCP servers, (b) fall back to `codex exec --json` (which has
native MCP via `codex mcp add`), (c) ship without MCP for thinking.

**Recommendation:** ship PiKernel for thinking without MCP first;
revisit if a specific MCP-only workflow surfaces.

#### Working directory

Pi has no `--cwd` / `--workdir` flag. It uses `process.cwd()` at
launch. The wrapper does `subprocess.run([..., "pi", ...],
cwd=mind_dir)` and the child process inherits.

Sessions are auto-organized by CWD under
`~/.pi/agent/sessions/<cwd-encoded>/`. We'd disable session
persistence via `--no-session` for thinking wakes (each wake is
one-shot; Alice owns its own session/state model).

#### Built-in tools and tool allowlist

`pi --tools <comma-list>` mirrors Alice's `allowed_tools` field on
`KernelSpec`. Built-in names: `read`, `bash`, `edit`, `write`,
`grep`, `find`, `ls`. The wrapper passes `--tools` constructed
from `KernelSpec.allowed_tools`.

#### Compaction — disable pi's, keep Alice's

Add to `~/.pi/agent/settings.json`:

```json
{
  "compaction": { "enabled": false }
}
```

Alice's `SessionCompaction` handler stays authoritative. Avoids
dueling compaction loops.

#### AGENTS.md / CLAUDE.md auto-discovery

Pi auto-loads `AGENTS.md` and `CLAUDE.md` from CWD and ancestors
unless `--no-context-files` is passed. Since the wrapper sets
`cwd=mind_dir` and `mind/CLAUDE.md` exists, pi will pick it up
automatically. **Don't pass `--no-context-files`** — we want this
behavior.

#### Runtime cost (rough)

Pi cold-start: ~1-2 seconds observed (node startup + module
loading). Comparable to claude_agent_sdk's `claude` CLI startup
(~1-3s). Not free, but tolerable for thinking wakes that run for
tens of seconds.

#### Auth file shape

`~/.pi/agent/auth.json` after `/login`:

```json
{
  "openai-codex": {
    "type": "oauth",
    "access_token": "...",
    "refresh_token": "...",
    "expires_at": "..."
  }
}
```

(Approximate — confirmed by docs but not yet observed live.) For
container deployment we mount this file from host into the worker.

## Architectural plan (next document)

Given the verdict, the next plan extracts a `Kernel` Protocol
from `alice_core/kernel.py:AgentKernel` and adds a `PiKernel` impl
in a new `alice_pi/` package. Sketch:

```
src/alice_core/kernel.py    Kernel Protocol (extracted, not new)
                            AgentKernel       (Anthropic via claude_agent_sdk)
src/alice_pi/kernel.py      PiKernel          (subprocess pi --mode json)
src/alice_pi/transport.py   subprocess + JSONL parser
src/alice_pi/spec.py        PiKernelSpec (model, tools, session handling)
```

Hemisphere selection in `alice_thinking/wake.py`: read
`model_config.thinking.backend` — `subscription`/`api`/`bedrock`
goes to AgentKernel as today; new value `pi` (or `codex` as alias)
instantiates PiKernel.

`model.yml` schema gains `backend: pi` and a top-level `pi:`
section (binary path, settings overrides) — to be designed in the
follow-up plan.

Estimated effort for the follow-up plan: ~3 days
(Protocol extraction + PiKernel + transport + tests + worker
image + skill pre-render integration).

## Open questions — now resolved

All five Phase-1 questions resolved during the spike:

1. **Auth file actual shape.** ✅ Resolved — see Phase 1 above.
   Codex CLI: `tokens.{access_token, refresh_token, account_id}`.
   Pi: `openai-codex.{access, refresh, expires, accountId}`. JWT
   exp gives `expires`. Translation script in Phase 1 section.
2. **Codex model strings.** ✅ Resolved — provider is `openai-codex`,
   models are bare names: `gpt-5.1`, `gpt-5.1-codex-max`,
   `gpt-5.1-codex-mini`, `gpt-5.2`, `gpt-5.2-codex`,
   `gpt-5.3-codex`, `gpt-5.3-codex-spark`, `gpt-5.4`,
   `gpt-5.4-mini`, `gpt-5.5`. All support thinking, all 272K
   context.
3. **Does `log-meal` fire?** ✅ Resolved — yes, pi's model picks
   it correctly from the description. See Phase 2 above.
4. **Skill name validation strictness.** ⚠️ Partial — pi WARNS on
   violations but the strict YAML parser HARD-REJECTS
   description-level YAML errors (caveat A in Phase 2). Skills
   with bad frontmatter don't load at all.
5. **Thinking-shaped end-to-end run.** Deferred to PiKernel
   implementation. The basic plumbing works; thinking-specific
   behavior (multi-tool turns, vault writes, MCP-or-not) gets
   exercised once the wrapper exists.

## Remaining open questions (for the implementation plan)

1. **Token-refresh client_id compatibility.** Untested. Pi's
   hardcoded `CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"`. If
   Codex CLI uses a different client_id, refresh fails after the
   10-day window and the operator must `codex login` + bridge
   again. Worth grepping Codex CLI source on day 1 of
   implementation to know whether to plan for rotation.
2. **Skill-rendering Python implementation.** The spike used pi's
   bundled JS `yaml` package to re-emit strict frontmatter. The
   Python equivalent is `ruamel.yaml` or
   `yaml.safe_dump(..., default_style='"')`. Verify the chosen
   library produces frontmatter that pi parses cleanly.
3. **Compaction interaction.** Pi has built-in compaction. Alice
   has its own `SessionCompaction` handler. The wrapper should
   set `compaction.enabled: false` in `~/.pi/agent/settings.json`
   so they don't fight. Test that disabling pi's compaction
   leaves the agent in a usable state for long-running sessions.

## What this spike did NOT touch

- No Alice code changes (no `src/alice_pi/` yet).
- No `model.yml` modifications.
- No worker compose / image work.
- No `git commit` — the report sits in `docs/refactor/` but isn't
  committed yet; Jason reviews and commits when ready.

## Files / artifacts on disk after this spike

- `~/.pi/agent/settings.json` — written, points at Alice skills
  dir at `data/alice-mind/.claude/skills`.
- `~/.pi/agent/auth.json` — populated via the codex→pi bridge.
  Working access token (10-day window) for `openai-codex`
  provider. NOT in git.
- `~/.pi/agent/auth.json.empty.bak` — pre-spike backup (`{}`).
- `/tmp/pi-spike-skills/log-meal/` — temporary directory used to
  prove the rendered SKILL.md works. Safe to delete; will not
  be re-created by Alice.
- `npm install -g @mariozechner/pi-coding-agent` — installed
  globally in nvm at `~/.nvm/versions/node/v24.12.0/lib/node_modules/@mariozechner/pi-coding-agent`.
- bundled docs at
  `/home/cronqj/.nvm/versions/node/v24.12.0/lib/node_modules/@mariozechner/pi-coding-agent/docs/`
  — useful reference once we start writing PiKernel
- pi-ai source (Codex provider impl):
  `…/node_modules/@mariozechner/pi-ai/dist/providers/openai-codex-responses.js`
  and `…/utils/oauth/openai-codex.js`. Reference for exact
  request shapes and OAuth flow.

## Cleanup if we abandon

```bash
npm uninstall -g @mariozechner/pi-coding-agent
rm -rf ~/.pi/agent/
rm -rf /tmp/pi-spike-skills/
```
