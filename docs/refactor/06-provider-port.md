# 06 — Backend selection (LiteLLM + Bedrock)

## Problem

The runtime supports exactly two auth/backend paths today, and the
selection happens implicitly via env-var presence:

`src/alice_core/auth.py:14-17`:

> The mode is picked implicitly: presence of `ANTHROPIC_BASE_URL` or
> `ANTHROPIC_API_KEY` selects `api`; otherwise `subscription`. When
> `api` is active we explicitly clear `CLAUDE_CODE_OAUTH_TOKEN` from
> the subprocess env — the CLI gets confused if both are set.

The two modes today:

- **subscription** — Anthropic Max-plan OAuth
  (`CLAUDE_CODE_OAUTH_TOKEN`).
- **api** — direct Anthropic API or any Anthropic-compatible proxy
  (e.g. LiteLLM) via `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` +
  optional `ANTHROPIC_AUTH_TOKEN`.

**The Claude Agent SDK supports more backends than we expose.** The
SDK itself can route to:

- Anthropic API (subscription or API key) — what we have.
- Anthropic-compatible proxies (LiteLLM, etc.) — half-wired (env-var-
  only, no first-class config).
- **AWS Bedrock** — via `CLAUDE_CODE_USE_BEDROCK=1` + AWS credentials
  + Bedrock-specific model IDs. **Not wired today.**
- Google Vertex AI — via `CLAUDE_CODE_USE_VERTEX=1` + GCP creds.
  **Not wired; not in scope for this plan.**

The user's stated goal: keep the Claude Agent SDK as the only kernel
implementation. Add **LiteLLM** (more first-class than today's env-only
opt-in) and **Bedrock** as supported backends. Make backend selection
config-driven, not implicit-from-env-vars.

### Concrete pain points

1. **Backend selection is implicit.** A reader of `alice.env` cannot
   tell *which* backend the daemon will use without manually working
   through the env-var precedence in `auth.py:99-104`. There's no
   `backend: bedrock` line they can see.

2. **No per-hemisphere backend selection.** Speaking and thinking
   share whatever auth mode `auth.py` resolves. There's no way to say
   "thinking runs on Bedrock for cost; speaking stays on subscription
   for low latency."

3. **Bedrock isn't wired.** Adding it today requires hand-editing the
   container's env (set `CLAUDE_CODE_USE_BEDROCK=1`, set
   `AWS_REGION`, mount AWS creds), discovering the right model IDs
   (`anthropic.claude-sonnet-4-5-20250929-v1:0` rather than
   `claude-sonnet-4-6`), and hoping nothing in `auth.py`'s
   subscription-mode-clearing trips it up.

4. **Model IDs are env-coupled.** `claude-sonnet-4-6` is the
   subscription/API name; the Bedrock equivalent has a different
   shape. The model name lives in `mind/config/alice.config.json` →
   speaking config. If you want to swap from subscription to Bedrock
   for cost, you also have to swap the model name, which lives in a
   different file. Two-step config change for one logical operation.

5. **The viewer makes its own LLM calls.** `alice_viewer/narrative.py`
   directly imports `claude_agent_sdk` and calls `query()`. It honors
   the same env vars (so it'll go through whatever auth the daemon
   uses), but its model name is hardcoded — `narrative.py:211, 402`
   set the model in code. Not config-driven.

## Goal

After this plan:

- **Backend selection is declarative.** `mind/config/model.yml`
  (or merged into `alice.config.json` — see open questions) names the
  backend explicitly: `backend: subscription | api | bedrock`.
- **Three backends are first-class:**
  - `subscription` — Anthropic Max OAuth (today).
  - `api` — Anthropic API key + optional LiteLLM proxy URL (today,
    promoted to first-class config).
  - `bedrock` — AWS Bedrock via `CLAUDE_CODE_USE_BEDROCK=1` + AWS
    creds. **New.**
- **Per-hemisphere backend selection.** Speaking and thinking can run
  on different backends. The kernel construction in each hemisphere's
  factory reads its own config block.
- **Model IDs live with the backend config.** `model.yml`'s
  `speaking.model` / `thinking.model` is whatever the chosen backend
  expects (subscription/API: `claude-opus-4-7`; Bedrock:
  `anthropic.claude-opus-4-..-v1:0`). The same field, the right value
  per backend.
- **Auth resolution handles all three modes.** `auth.py` extends to
  understand `bedrock` mode (sets `CLAUDE_CODE_USE_BEDROCK=1`,
  preserves AWS env vars, clears the others).
- **The kernel doesn't change.** It still calls the Claude Agent SDK.
  No `ModelProvider` protocol, no `Tool` redefinition, no event-shape
  translation. The backend is auth + env, not a Python abstraction.
- **Smoke tests for each backend** (paid, gated by `pytest -m smoke`)
  prove the wiring works end-to-end.

## Design

### `mind/config/model.yml` schema

```yaml
# Backend selection per hemisphere.
# Each hemisphere may pick a different backend. Models named here are
# whatever the chosen backend expects.

speaking:
  backend: subscription              # subscription | api | bedrock
  model: claude-opus-4-7             # subscription/API model id
  # Per-hemisphere overrides — fall back to top-level "backends:" if absent.

thinking:
  backend: bedrock
  model: anthropic.claude-sonnet-4-5-20250929-v1:0
  region: us-east-1                  # AWS region for Bedrock

# Backend-level configuration. Inherited by hemispheres unless overridden.
backends:
  subscription:
    # No backend-level fields needed — auth comes from CLAUDE_CODE_OAUTH_TOKEN
    # in alice.env.
  api:
    base_url: https://litellm.example.com/v1   # optional; omit for direct Anthropic
    auth_header: bearer                         # bearer | x-api-key (default: x-api-key)
  bedrock:
    region: us-east-1                # default region; per-hemisphere can override
    profile: alice-prod              # optional AWS profile name; default uses creds chain
```

The viewer is a **third hemisphere** — it makes its own LLM calls
from `narrative.py` and `run_summary.py`. The same `model.yml`
configures it:

```yaml
viewer:
  backend: subscription              # may differ from speaking/thinking
  narrative_model: claude-haiku-4-5-20251001
  run_summary_model: claude-sonnet-4-6
  cue_model: claude-haiku-4-5-20251001  # for the cue runner once it ships
```

(Today these models are hardcoded in `narrative.py:211, 402` and
`run_summary.py`.) The viewer reads `model.yml` at FastAPI startup
and exposes it via `app.state.model_config`.

**Open: should the viewer share speaking's backend by default?**
Pragmatically: yes. The viewer is read-only narrative work, low
volume; matching speaking's auth saves separate cred setup. If the
user wants viewer on a cheaper backend (e.g. Bedrock for batch
narrative summarization), they explicitly set `viewer.backend`.

### `auth.py` extends to a third mode

```python
AuthMode = Literal["subscription", "api", "bedrock", "none"]

@dataclass(frozen=True)
class AuthEnv:
    mode: AuthMode
    oauth_token: str = ""
    api_key: str = ""
    auth_token: str = ""
    base_url: str = ""
    aws_region: str = ""
    aws_profile: str = ""
```

Resolution rules (in priority order):

1. If `model.yml` declares `backend: bedrock` for this process's
   hemisphere → mode = `bedrock`. The SDK env vars set:
   `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION=<region>`, optionally
   `AWS_PROFILE=<profile>`. Subscription / API vars cleared.
2. If `model.yml` declares `backend: api` → mode = `api`. Set
   `ANTHROPIC_BASE_URL` (from config or `alice.env`),
   `ANTHROPIC_API_KEY`, optionally `ANTHROPIC_AUTH_TOKEN`. Clear
   subscription + Bedrock vars.
3. If `model.yml` declares `backend: subscription` → mode =
   `subscription`. Set `CLAUDE_CODE_OAUTH_TOKEN` from `alice.env`.
   Clear API + Bedrock vars.
4. **Backwards-compat:** if `model.yml` doesn't exist or doesn't
   declare a backend, fall back to today's implicit-from-env logic.
   (`ANTHROPIC_*` present → api; `CLAUDE_CODE_OAUTH_TOKEN` present →
   subscription.)

### Per-hemisphere environment scoping

Speaking and thinking are separate processes, each calling
`ensure_auth_env()` at startup. Each one passes its own hemisphere
identifier so `auth.py` reads the right block from `model.yml`:

```python
auth = ensure_auth_env(hemisphere="speaking")  # in daemon factory
auth = ensure_auth_env(hemisphere="thinking")  # in wake.py
auth = ensure_auth_env(hemisphere="viewer")    # in viewer main
```

This keeps the env-var-mutation contract: each process sets up its
own SDK environment based on its own backend config.

### AWS credentials sourcing

Bedrock requires AWS credentials. The runtime supports the standard
AWS credential chain — in priority order:

1. Env vars: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` +
   optional `AWS_SESSION_TOKEN`. Settable in `alice.env`.
2. Profile: `AWS_PROFILE` (or `model.yml`'s `bedrock.profile`)
   pointing at `~/.aws/credentials`. The container needs
   `~/.aws/` mounted (read-only); add to docker-compose.
3. Instance profile: if running on EC2, the SDK picks up
   instance-attached credentials. Not relevant for the local
   sandbox; relevant if someone deploys Alice to AWS.

`auth.py` doesn't manage AWS creds itself — it just doesn't *clear*
the AWS env vars when in Bedrock mode. The SDK + boto3 credential
chain does the rest.

### Per-container credential requirements

Credentials are needed in **whichever container runs the LLM call**.
Because there are three LLM-using processes (speaking and thinking
in `alice-worker`, narrative + run-summary in `alice-viewer`),
credential mounts must be configured **per container, per backend**,
not as a global "if any hemisphere uses Bedrock, mount AWS creds."

Matrix of which container needs what:

| Container        | Backend used by         | Required credentials                                |
|------------------|--------------------------|-----------------------------------------------------|
| `alice-worker`   | speaking + thinking      | `CLAUDE_CODE_OAUTH_TOKEN` (subscription) ∪ `ANTHROPIC_*` (api) ∪ AWS creds (Bedrock), per the backends those two hemispheres pick |
| `alice-viewer`   | viewer.narrative_model + viewer.run_summary_model + cue_model | Same matrix, scoped to the viewer's chosen backend(s) |

If `viewer.backend: bedrock` is set, the viewer container needs
`~/.aws/:/home/alice/.aws/:ro` mounted independently of whether the
worker does. `docker-compose.yml` gets optional commented mount
blocks for both containers.

Mind-scaffold's `model.yml.example` and the post-Phase-8 docs list
"if you set `<container>.backend: bedrock`, add the AWS mount to
that container's compose block" rather than just "if you use
Bedrock anywhere."

`auth.py`'s mode resolution is **per-process, not per-container** —
speaking and thinking call `ensure_auth_env(hemisphere=...)`
independently and each one mutates only its own subprocess env.
Cross-contamination is not a risk because they're separate processes
under s6 supervision.

### Mind-scaffold updates

`templates/mind-scaffold/config/` gets a `model.yml.example` showing
all three backends commented out:

```yaml
# Pick one backend per hemisphere. Subscription is the default if
# this file is omitted.

speaking:
  backend: subscription
  model: claude-opus-4-7

thinking:
  backend: subscription
  model: claude-sonnet-4-6

# Examples (uncomment to use):
#
# # LiteLLM proxy:
# speaking:
#   backend: api
# backends:
#   api:
#     base_url: https://litellm.your-proxy.example.com/v1
#
# # Bedrock:
# thinking:
#   backend: bedrock
#   model: anthropic.claude-sonnet-4-5-20250929-v1:0
#   region: us-east-1
```

`bin/alice-init` prompts for backend choice during setup and writes
the appropriate `model.yml`.

### Viewer

`alice_viewer/narrative.py:200, 395` and `run_summary.py:154 region`
have hardcoded model strings. After this plan they read from
`model.yml`'s `viewer:` block. Mechanism: viewer's `main.py`
loads `model.yml` at startup, exposes via `app.state.model_config`,
narrative + run_summary read from it.

### What stays the same

- `KernelSpec` is unchanged. It still has `model: str` etc.
- The kernel still calls `claude_agent_sdk.query()`.
- Tools stay as MCP-defined (`@tool`, `SdkMcpTool`). No `Tool`
  protocol abstraction.
- The event stream from the SDK (`AssistantMessage` / `ResultMessage`
  / etc.) is unchanged.
- Session resume, MCP tool semantics, subagent spawning — all
  unchanged. Whatever the SDK supports under each backend, we get.

### Alternatives considered

- **Build a `ModelProvider` abstraction layer** (the previous
  iteration of this plan). Rejected per user instruction — keep the
  SDK; add backends instead. The SDK already abstracts backends; we
  just need to expose its existing capability through config.

- **Use LiteLLM as the only backend** — point the SDK at a LiteLLM
  proxy that fans out to subscription/API/Bedrock/etc. Reduces
  surface area but adds a single-point-of-failure proxy. Doesn't
  remove the need for first-class Bedrock since LiteLLM doesn't
  always cover all features (e.g. extended-thinking blocks). Stay
  with native SDK backends; let users opt into LiteLLM via the
  `api` backend's `base_url`.

- **Keep selection implicit-from-env.** Today's design. Cheap, but
  the user explicitly wants config-driven and per-hemisphere. Reject.

- **Put backend config in `alice.config.json`.** That file's the
  place for behavioral knobs Alice can self-tune (model,
  quiet_hours, working_context_token_budget). Backend selection is
  infra, not behavior — it's not something Alice should change for
  herself. Separate file (`model.yml`) signals different lifecycle.

- **Skip Bedrock; just promote LiteLLM.** Most of the user value is
  LiteLLM (already half-wired). But adding Bedrock costs roughly
  the same as promoting LiteLLM (it's auth.py + env vars + smoke
  test). Do both.

## Phases

### Phase 1 — `model.yml` schema + loader

**Goal:** Configuration shape exists. Nothing consumes it yet.

**Changes:**
- `src/alice_core/model_config.py` — `ModelConfig` dataclass +
  `load(mind_path: Path) -> ModelConfig`.
- `templates/mind-scaffold/config/model.yml.example` — commented
  example.
- `data/alice-mind/config/model.yml` — created during this plan with
  the user's actual current backend (subscription, today).

**Validation:** `tests/test_model_config.py`:
- `test_load_minimal_config` — speaking + thinking, no backends
  block.
- `test_load_full_config` — all three backends, per-hemisphere
  overrides.
- `test_load_missing_file_returns_default_subscription_config` —
  back-compat for minds without `model.yml`.
- `test_load_invalid_backend_raises_clear_error`
- `test_per_hemisphere_inherits_from_top_level_backends`

**Exit criteria:** Config loads; suite green.

---

### Phase 2 — `auth.py` extends to Bedrock mode

**Goal:** Auth resolution understands `bedrock`. Nothing reads the
new mode yet (we still implicitly pick from env vars).

**Changes:**
- `AuthMode` adds `"bedrock"`.
- `AuthEnv` adds `aws_region: str` and `aws_profile: str`.
- `find_auth_env()` and `ensure_auth_env()` accept an optional
  `mode_hint: AuthMode | None` argument. When `mode_hint == "bedrock"`,
  set `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION` from config or
  `AWS_REGION` env, optionally `AWS_PROFILE`. Clear other modes' vars.

**Validation:** `tests/test_auth.py` (new — there isn't one yet):
- `test_auth_env_subscription_mode_sets_oauth_only`
- `test_auth_env_api_mode_sets_api_vars_clears_oauth`
- `test_auth_env_bedrock_mode_sets_use_bedrock_clears_others`
- `test_auth_env_bedrock_mode_preserves_aws_creds`
- `test_auth_env_with_no_hint_falls_back_to_implicit_resolution`

**Exit criteria:** Auth resolution supports all three modes via
`mode_hint`; back-compat path still works for minds without
`model.yml`.

---

### Phase 3 — Speaking daemon reads `model.yml`

**Goal:** Speaking's startup picks its backend + model from
`model.yml`, with implicit-from-env as fallback.

**Changes:**
- `factory.py` (or `daemon.py` startup): load `ModelConfig`,
  resolve `speaking.backend`, call `ensure_auth_env(mode_hint=...)`,
  set `KernelSpec.model = config.speaking.model`.
- Backwards compat: if `model.yml` doesn't declare speaking's block,
  use today's implicit env detection + `alice.config.json`'s
  `speaking.model` value.

**Validation:**
- `tests/test_daemon.py::test_kernel_spec_model_from_model_yml` —
  fixture mind with `speaking.model: claude-sonnet-test`; daemon
  builds spec with that model.
- `tests/test_daemon.py::test_falls_back_to_alice_config_when_model_yml_absent`
- Manual: `bin/alice -p "ping"` against deployed worker still works
  on subscription.

**Exit criteria:** Speaking is config-driven; behavior unchanged for
existing minds.

---

### Phase 4 — Thinking + viewer read `model.yml`

**Goal:** Same for the other two LLM-using processes.

**Changes:**
- `wake.py` reads `thinking.backend` + `thinking.model`.
- `alice_viewer/main.py` loads `model.yml` at startup; exposes via
  `app.state.model_config`.
- `narrative.py` + `run_summary.py` read model strings from
  `app.state.model_config.viewer.*` instead of hardcoded constants.

**Validation:**
- `tests/test_thinking_wake.py::test_wake_kernel_spec_model_from_config`
- Manual: viewer's `/narrative` endpoint still produces a summary;
  log line at startup reports which backend/model is in use.

**Exit criteria:** All three LLM-using processes are config-driven.

---

### Phase 5 — Bedrock smoke test **(paid)**

**Goal:** Prove Bedrock works end-to-end.

**Changes:**
- `tests/smoke/test_bedrock.py` (gated `@pytest.mark.smoke`): set up
  a fixture mind with `thinking.backend: bedrock`, run `bin/alice-think
  --quick`, assert it returns `QUICK-OK`.
- Document AWS-cred prerequisites in
  `docs/refactor/06-provider-port.md` (this file).
- Update `docker-compose.yml` to optionally mount `~/.aws/`
  read-only (commented; users opt in).

**Validation:** `pytest -m smoke tests/smoke/test_bedrock.py`
returns 0 when AWS creds + Bedrock model access are set up.

**Exit criteria:** A user with AWS Bedrock access can flip
`thinking.backend: bedrock` and `bin/alice-think --quick` succeeds.

---

### Phase 6 — LiteLLM smoke test **(paid)**

**Goal:** Same for LiteLLM proxy.

**Changes:**
- `tests/smoke/test_litellm.py` — fixture mind with `api` backend
  pointed at a LiteLLM URL.

**Validation:** `pytest -m smoke tests/smoke/test_litellm.py`
returns 0 against a working LiteLLM endpoint (user-supplied URL +
key).

**Exit criteria:** Documented LiteLLM path verified.

---

### Phase 7 — `bin/alice backend` introspection

**Goal:** A user can ask "which backend am I on?" without grepping.

**Changes:**
- `bin/alice backend show` → prints resolved backend + model per
  hemisphere from `model.yml`.
- `bin/alice backend test [hemisphere]` → runs a quick LLM call on
  the chosen hemisphere's backend, reports success/failure.

**Validation:** `tests/test_backend_cli.py`:
- `test_backend_show_outputs_per_hemisphere_config`
- `test_backend_show_handles_missing_model_yml`

**Exit criteria:** Inventory tooling works.

---

### Phase 8 — Cleanup + docs

**Goal:** Final naming, env-var cleanup, docs.

**Changes:**
- `templates/mind-scaffold/CLAUDE.md` documents the three backends,
  cred setup, when to choose which.
- `config/alice.env.example` references `model.yml` as the
  recommended path; existing env vars stay supported as overrides.
- `bin/alice-init` prompts for backend during setup.

**Validation:** Manual review of docs.

**Exit criteria:** A new user reading `bin/alice-init`'s output knows
how to pick a backend and what creds each needs.

---

## Tests

### Existing tests this plan must keep green

- `tests/test_kernel.py` — kernel calls don't change; existing tests
  unaffected.
- `tests/test_daemon.py` — Phase 3 changes spec construction; tests
  update to either provide a fixture `model.yml` or rely on the
  back-compat path.
- `tests/test_a2a_transport.py`, `tests/test_discord_transport.py` —
  no kernel-level changes; should pass throughout.

### New tests this plan introduces

- `tests/test_model_config.py` (Phase 1):
  - `test_load_minimal_config`
  - `test_load_full_config`
  - `test_load_missing_file_returns_default_subscription_config`
  - `test_load_invalid_backend_raises_clear_error`
  - `test_per_hemisphere_inherits_from_top_level_backends`

- `tests/test_auth.py` (Phase 2 — new file):
  - `test_auth_env_subscription_mode_sets_oauth_only`
  - `test_auth_env_api_mode_sets_api_vars_clears_oauth`
  - `test_auth_env_bedrock_mode_sets_use_bedrock_clears_others`
  - `test_auth_env_bedrock_mode_preserves_aws_creds`
  - `test_auth_env_with_no_hint_falls_back_to_implicit_resolution`
  - `test_ensure_auth_env_clears_wrong_mode_vars`

- `tests/test_daemon.py` extension (Phase 3):
  - `test_kernel_spec_model_from_model_yml`
  - `test_falls_back_to_alice_config_when_model_yml_absent`

- `tests/test_thinking_wake.py` extension (Phase 4):
  - `test_wake_kernel_spec_model_from_config`

- `tests/test_backend_cli.py` (Phase 7):
  - `test_backend_show_outputs_per_hemisphere_config`
  - `test_backend_show_handles_missing_model_yml`

- `tests/smoke/test_bedrock.py` (Phase 5; `@pytest.mark.smoke`):
  - `test_quick_wake_against_bedrock_returns_QUICK_OK`

- `tests/smoke/test_litellm.py` (Phase 6; `@pytest.mark.smoke`):
  - `test_quick_wake_against_litellm_returns_QUICK_OK`

## Risks & non-goals

### Risks

- **Bedrock model IDs are different from subscription/API model
  IDs.** A common confusion: setting `thinking.backend: bedrock`
  while leaving `thinking.model: claude-sonnet-4-6` (the
  subscription name) → SDK errors at first call. The validator in
  `model_config.load()` should warn when a model name doesn't match
  the backend's expected pattern (Bedrock IDs start with
  `anthropic.` and end with `-v\d+:0`). Not a hard error (users may
  legitimately use custom inference profiles), but a startup
  warning.

- **AWS cred setup is a foot-gun.** Wrong cred chain → SDK fails at
  first call with a generic error. Phase 5's smoke test catches this
  for the user; the daemon should also surface a clear error at
  startup if `bedrock` mode is configured but no AWS creds are
  resolvable (`boto3.Session().get_credentials()` returns None).

- **Environment isolation between processes.** Speaking and thinking
  run in the same container today. If they pick different backends,
  `auth.py` mutates `os.environ` for whichever process is calling.
  No cross-contamination because they're separate processes (s6
  manages each as its own service). Verify by running speaking on
  subscription + thinking on Bedrock on the same worker; check
  thinking's env doesn't leak.

- **Subscription mode is rate-limited per-account.** Today both
  hemispheres share that limit. Moving thinking to API or Bedrock
  shifts cost from subscription to per-token billing. Make sure the
  user understands the cost model when picking. Document in Phase 8.

- **The viewer makes synchronous LLM calls.** Switching the viewer
  to Bedrock could change latency profile. Test before flipping.

### Non-goals

- **Vertex AI support.** SDK supports it via `CLAUDE_CODE_USE_VERTEX`.
  Out of scope for this plan; same shape as Bedrock if added later.
- **Local model support** (Ollama, llama.cpp, etc.). Different
  protocol entirely; would require the `ModelProvider` abstraction
  the previous iteration of this plan proposed. Out of scope.
- **Replacing the Claude Agent SDK.** Explicitly: not happening.
- **Provider-agnostic tools or skills.** Tools stay MCP-via-SDK.
  Skills stay markdown-loaded-by-SDK. Plan 07 (skills) no longer
  has a "Phase 7" for non-SDK provider exposure — see plan 07
  open questions.
- **Cost dashboards.** Telemetry stays as today (cost reported per
  turn in `ResultMessage.usage`). No backend-aggregated cost view.

## Open questions

1. **One file or merged into `alice.config.json`?**
   `alice.config.json` is the place for *behavioral* knobs Alice can
   self-tune. Backend selection is *infra*; Alice shouldn't be able
   to swap her own model provider mid-session. Different lifecycle
   → different file. **Recommendation: separate `model.yml`.**

2. **Where does `model.yml` live?**
   - `mind/config/model.yml` — alongside `alice.config.json` and
     `principals.yaml`. **Recommended.**
   - `mind/model.yml` (top level) — visible but cluttered.

3. **Does `bin/alice-init` prompt for backend during setup, or
   default to subscription?**
   Default subscription (current behavior). Prompt only when the
   user asks for "advanced setup" or runs `bin/alice-init backend`
   explicitly. Most users will never touch this.

4. **Should `auth.py` handle AWS creds, or delegate fully to boto3's
   chain?**
   Delegate. `auth.py` only manages `CLAUDE_CODE_USE_BEDROCK`,
   `AWS_REGION`, `AWS_PROFILE`. Actual cred resolution is boto3's
   problem. Keeps `auth.py` focused.

5. **Per-hemisphere AWS regions?**
   Yes — `model.yml`'s per-hemisphere block can override
   `bedrock.region`. Useful if speaking runs in `us-east-1` for
   latency and thinking runs in a cheaper region.

6. **What about the cue runner?** (See `data/alice-mind/cortex-memory/research/2026-04-28-haiku-cue-runner-auth-investigation.md`.)
   Out of scope for this plan but the same pattern applies — the
   cue runner reads a model from `model.yml` (`viewer.cue_model:
   claude-haiku-4-5-20251001`) and uses the same auth resolution.
