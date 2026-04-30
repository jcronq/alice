# 05 — Personae config + system prompt injection

**Depends on plan 04 (prompts package).** This plan substitutes
agent + user identity into rendered prompts; without the prompts
package there's nowhere to substitute into.

## Problem

Two intertwined problems, called out by the user:

### 1. Persona files are not actually injected

`templates/mind-scaffold/HEMISPHERES.md:160` claims:

> System-prompt injection (once per speaking-Alice process lifetime):
> SOUL.md, IDENTITY.md, CLAUDE.md, USER.md, and a snapshot of
> memory/claude-code-project/MEMORY.md. Not re-injected per turn —
> we are smart about this.

**This is a lie about the implementation.** A grep across the runtime
for `system_prompt=` and `append_system_prompt=` returns one hit —
`src/alice_speaking/_sanity.py:31`, a smoke test. The kernel
(`src/alice_core/kernel.py`) never sets either field on
`ClaudeAgentOptions`. The mechanism that actually keeps the agent
"in character" is:

- The kernel runs with `cwd=alice-mind`.
- Claude Code's CLI auto-loads `CLAUDE.md` from `cwd`.
- `CLAUDE.md` references `IDENTITY.md`, `SOUL.md`, `USER.md` as wikilinks
  — the agent reads them via the Read tool *if she remembers to*.
- Thinking's `wake.py:_build_prompt` inlines `inner/directive.md` (only
  — not SOUL/IDENTITY/USER).

Pull the rug on Claude Code's cwd-auto-load (e.g. swap to a different
provider per plan 06) and the agent's identity evaporates.

### 2. Names are hardcoded, on both sides of the conversation

**The agent's name "Alice"** is baked into ~30 sites:

- Viewer chrome: `templates/base.html:6,19`, `main.py:27`
  (`FastAPI(title="Alice Viewer")`), `static/style.css:1`,
  `templates/memory.html:74`, `templates/narrative.html:7`,
  `static/events.js:318`.
- LLM prompts in the viewer: `narrative.py:128, 130-132, 138-140,
  353, 481, 483` — full multi-paragraph prompts hardcoding "Speaking
  Alice (Opus)" / "Thinking Alice (Sonnet)" by name.
- A2A transport: `transports/a2a.py:171` (`agent_name: str = "Alice"`
  default), `a2a.py:345` ("Talk to Alice in plain English…").
- Tool descriptions (the agent's view of her own tools):
  `tools/inner.py:64,127`, `tools/memory.py:5,11,45`,
  `tools/config_tools.py:43,59`.
- Render layer: `render.py:1,6,14,34,47`.
- Log labels: `aggregators.py:611,718` (`"Thinking Alice"` as a sender
  string), `run_summary.py:154`.

**The user's role "owner"** is in ~10 sites, with a worse problem —
the word frames the human-AI relationship as ownership:

- `principals.py:212` — `fallback_cli_principal_id: str = "owner"`,
  default display name `"Owner (local CLI)"`.
- `compaction.py:35,37` — *"open questions and pending tasks the
  **owner** or…"*, *"the **owner**'s current state"*.
- `narrative.py:131,140,483` — *"voices Signal to the **owner** and
  their trusted contacts"*.
- `tools/messaging.py:14` — recipient example shown to the agent:
  `'"owner"'`.
- `aggregators.py:598,671,759,995`, `templates/memory.html:84` —
  cosmetic, but feeds the codebase tone.

These are all places where the agent should be saying *"Jeremy"* (or
whatever name the user configured), not *"the owner"*; or saying
*"Alice"* (today's default) only because the user didn't change it,
not because it's hardcoded.

## Goal

After this plan:

- **One config source of truth** for agent + user identity:
  `mind/personae.yml` (or split into `agent.yml` + `user.yml` —
  see open questions). Loaded once per process.
- **A real injection point in the kernel** — `KernelSpec` gains an
  `append_system_prompt: str | None` field; the kernel passes it to
  `ClaudeAgentOptions.append_system_prompt`. Personae render into
  this field at process start.
- **Prompt templates use `{{ agent.name }}` / `{{ user.name }}`**
  everywhere they previously hardcoded "Alice" / "owner".
- **Viewer templates use Jinja context** (`{{ agent.name }}` in HTML)
  for chrome.
- **Tool descriptions are templated** — rendered at MCP-server build
  time using the loaded personae, so `log-meal` doesn't have "Jason"
  baked into its description (this also intersects plan 07 — skills).
- **A2A AgentCard defaults** read from the personae config, not from
  a hardcoded `"Alice"` arg default.
- **The default principal display name** is the user's actual name,
  not `"Owner (local CLI)"`.
- **The `recipient="owner"` shape in `send_message` becomes
  `recipient="<user.name>"`** as the canonical form — the role name
  is the user's actual name. (See open questions for the back-compat
  path.)
- **Renaming the agent or the user is one config edit + one daemon
  restart.** Verifiable end-to-end: change `personae.yml`, restart,
  ask the agent "what's your name?", get the new name.

## Design

### `mind/personae.yml` schema

```yaml
agent:
  name: Alice
  pronouns: she/her
  tagline: "concise assistant with opinions"
  lineage: "named for A.L.I.C.E., the 1995 chatbot"
  voice:                          # optional voice/tone notes for prompts
    summary: "Executive-level assistant with department-head competence."
    rules:
      - "Be genuinely helpful, not performatively helpful."
      - "Have opinions."
      - "Be resourceful before asking."

user:
  name: Jeremy
  pronouns: he/him
  addressing: "first name"        # "first name" | "last name" | "nickname" | "honorific"
  honorific: null                 # only used when addressing == "honorific"
  relationship: "friend"          # purely descriptive — never "owner"
  about:                          # optional freeform context
    - "Software engineer."
    - "Lives in EDT/EST timezone."
```

All fields except `agent.name` and `user.name` are optional. Defaults
are sensible no-ops (e.g. missing `voice.rules` → no rules section in
the rendered system prompt).

### `src/alice_core/personae.py`

```
@dataclass(frozen=True)
class AgentPersona:
    name: str
    pronouns: str | None
    tagline: str | None
    lineage: str | None
    voice_summary: str | None
    voice_rules: tuple[str, ...] = ()

@dataclass(frozen=True)
class UserPersona:
    name: str
    pronouns: str | None
    addressing: str = "first name"
    honorific: str | None = None
    relationship: str | None = None
    about: tuple[str, ...] = ()

@dataclass(frozen=True)
class Personae:
    agent: AgentPersona
    user: UserPersona

def load(mind_path: Path) -> Personae:
    """Load personae.yml; raise PersonaeError if required fields missing."""

def system_prompt(personae: Personae) -> str:
    """Render the system-prompt-injection text from personae.yml.
    Uses the prompts package: prompts.load('meta.system_persona', ...).
    Returns a string suitable for ClaudeAgentOptions.append_system_prompt."""
```

### `KernelSpec.append_system_prompt`

```
@dataclass
class KernelSpec:
    model: str
    allowed_tools: list[str] = field(default_factory=list)
    cwd: Optional[pathlib.Path] = None
    mcp_servers: Optional[dict] = None
    resume: Optional[str] = None
    max_seconds: int = 0
    thinking: Optional[dict] = None
    append_system_prompt: Optional[str] = None  # NEW
```

`AgentKernel._build_options` passes it through:

```
def _build_options(self, spec: KernelSpec) -> ClaudeAgentOptions:
    kwargs = {...}
    if spec.append_system_prompt:
        kwargs["append_system_prompt"] = spec.append_system_prompt
    return ClaudeAgentOptions(**kwargs)
```

### Wiring sites

Six places consume personae:

1. **Speaking daemon's kernel calls** — `factory.py` builds `KernelSpec`
   with `append_system_prompt = personae.system_prompt(...)` at
   process start. Reused across turns.

2. **Thinking wake's kernel call** — same. Loaded once per wake (the
   wake is short-lived; reload-on-restart is fine).

3. **Prompt templates' default context** — every `prompts.load(...)`
   call gets `agent` and `user` automatically (per plan 04 §"Context
   defaults").

4. **Viewer Jinja context** — `alice_viewer.main` adds
   `app.state.personae = load_personae()` at startup; templates read
   `{{ personae.agent.name }}`, `{{ personae.user.name }}`.

5. **Tool descriptions (MCP)** — `tools/__init__.py:create_sdk_mcp_server`
   takes a `personae` arg; tool builders render their description
   strings via `prompts.load('tools.<name>.description', ...)` with
   the personae in context.

6. **A2A AgentCard** — `a2a.A2ATransport.__init__` reads
   `agent_name=personae.agent.name` (no hardcoded default).

### Default principal display name

`principals.py:213` (`fallback_cli_display_name: str = "Owner (local CLI)"`)
becomes:

```
fallback_cli_display_name = f"{personae.user.name} (CLI)"
```

(or, if the user prefers, just `personae.user.name`).

### `recipient="owner"` migration

The `send_message` tool today accepts `recipient="owner"` as a
sentinel meaning "the primary human." After this plan, the canonical
form is `recipient="<user.name>"` (e.g. `"Jeremy"`).

Two compatibility moves:

1. The principal book registers the user under the canonical name
   (`PrincipalRecord(id=personae.user.name.lower(), ...)`).
2. For at-most-one plan duration, register `"owner"` as an alias →
   maps to the same principal. Tool-description text shown to the
   agent uses the canonical name. Dropped at plan close.

### Alternatives considered

- **Two separate files (`agent.yml` + `user.yml`).** Cleaner conceptually
  but doubles the I/O and the config-loading edge cases. Single file
  with two top-level keys is simpler.

- **TOML or JSON instead of YAML.** YAML is already a runtime
  dependency (`pyyaml>=6.0` for `principals.yaml`). Adding TOML
  would mean another import. JSON is fine for machine-edited config
  but ugly for the multi-line freeform fields like `voice.summary`.
  Stick with YAML for hand-edit ergonomics.

- **Personae as part of `alice.config.json`.** Tempting (one config
  file). Rejected because the schemas are different in spirit —
  `alice.config.json` is behavioral knobs Alice can self-tune;
  personae is identity, hand-edited, rarely changed. Different
  files signal different lifecycle.

- **Inject by editing `IDENTITY.md` / `USER.md` and have the agent
  read them.** Today's de facto approach. Doesn't fix the problem;
  reading is voluntary and breaks under provider swap.

- **Use `system_prompt=` instead of `append_system_prompt=`.**
  `system_prompt` replaces the SDK's default system prompt entirely;
  `append_system_prompt` adds to it. We want to **append** —
  the SDK's default tells the model "you have these tools, here's
  how to call them," which we don't want to lose. Append.

## Phases

### Phase 1 — Personae loader, no consumers

**Goal:** Load `personae.yml`, parse, validate, expose. Nothing
consumes it yet.

**Changes:**
- `src/alice_core/personae.py` — `Personae`, `AgentPersona`,
  `UserPersona`, `load()`, `system_prompt()`.
- `templates/mind-scaffold/personae.yml` — example with `agent.name:
  Alice` and `user.name: Friend` (placeholder).
- `bin/alice-init` — copies the personae template if missing; prompts
  the user for `user.name`.
- `data/alice-mind/personae.yml` — created by hand or via
  `alice-init` re-run, with the actual user's name.

**Validation:** `tests/test_personae.py`:
- `test_load_minimal_personae` — only required fields → defaults fill
  in.
- `test_load_full_personae` — all fields parsed correctly.
- `test_load_raises_on_missing_required_field` — missing `agent.name`.
- `test_system_prompt_includes_agent_name`
- `test_system_prompt_includes_user_name`
- `test_system_prompt_includes_voice_rules_when_present`

**Exit criteria:** Loader works; suite green; nothing else changed.

---

### Phase 2 — Add `append_system_prompt` to `KernelSpec`

**Goal:** Kernel can accept a system prompt; nothing passes one yet.

**Changes:**
- `KernelSpec.append_system_prompt: Optional[str] = None`.
- `AgentKernel._build_options` passes it through when set.

**Validation:** `tests/test_kernel.py::test_kernel_passes_append_system_prompt_to_options`
— construct a `KernelSpec` with the field set, mock `query()`, assert
the captured `ClaudeAgentOptions` contains the expected
`append_system_prompt`.

**Exit criteria:** Kernel supports the field; existing tests green.

---

### Phase 3 — Wire personae into the speaking daemon **(behavior change, gated on plan 04 Phase 2)**

**Goal:** Speaking now sees the system-prompt injection.

**Gate:** Phase 3 must NOT ship before plan 04 Phase 2 lands (the
compaction prompt migrated to a template with `{{ user.name }}`
placeholders). Otherwise: the system prompt suddenly says "you are
Alice, talking with Jeremy" while compaction still says "the owner's
current state." Visible inconsistency the agent will notice and
attempt to reconcile, badly. CI gate: a test in `test_invariants.py`
that fails if `personae.system_prompt()` is non-empty AND any
templated prompt under `templates/speaking/` still contains the
literal substring `the owner` or `Alice` (assuming a non-default
fixture personae). Drop the gate test once Plan 04 Phase 2 lands and
the literals are gone.

**Changes:**
- `factory.py` (or daemon construction) loads personae, builds
  `system_prompt`, attaches to `KernelSpec`. All speaking turns now
  include it.

**This is a behavior change.** The agent will now actually see her
configured name + tagline + voice rules in the system prompt. Run a
manual smoke: `bin/alice -p "what's your name?"` — should return the
configured name.

**Validation:**
- `tests/test_daemon.py::test_kernel_spec_includes_system_prompt_from_personae`
— assert the spec the daemon constructs has a non-empty
`append_system_prompt`.
- Manual: deploy, run `bin/alice -p "what's your name and pronouns?"`,
  confirm the answer matches `personae.yml`.
- Manual: change `agent.name` to `"Eve"`, restart, ask again, confirm
  the answer changes.

**Exit criteria:** Speaking is persona-aware; one knob (the YAML
file) controls the agent's self-identification.

---

### Phase 4 — Same for thinking **(behavior change)**

**Goal:** `wake.py` loads personae and includes them in the wake's
kernel call.

**Changes:**
- `wake.py` loads personae (same loader from Phase 1).
- The wake's `KernelSpec` includes `append_system_prompt`.
- Per plan 03 (if shipped), each `Mode.kernel_spec(ctx)` reads from
  `ctx.personae`.

**Validation:**
- `tests/test_thinking_wake.py::test_wake_kernel_spec_includes_personae`
— same shape as Phase 3.
- Manual: `bin/alice-think --quick` — log shows the rendered system
  prompt was passed.

**Exit criteria:** Thinking wakes carry persona context.

---

### Phase 5 — Templated tool descriptions

**Goal:** Tool descriptions stop hardcoding "Alice" / "Jason".

**Changes:**
- Tool description strings in `tools/inner.py`, `tools/memory.py`,
  `tools/config_tools.py`, `tools/messaging.py` move to templates
  under `prompts/templates/tools/<tool_name>.description.md.j2`.
- `tools/__init__.py:create_sdk_mcp_server(personae=...)` takes
  personae and renders descriptions at MCP-server build time.

**Validation:** `tests/test_tools.py` (new):
- `test_tool_descriptions_render_with_agent_name`
- `test_tool_descriptions_render_with_user_name`
- `test_tool_description_for_each_tool_has_no_literal_alice` —
  greps the rendered descriptions for the literal string "Alice"
  (assuming `personae.agent.name != "Alice"` in the test fixture)
  and asserts none.

**Exit criteria:** No tool description literally contains the agent's
or user's name except via the personae-rendered output.

---

### Phase 6 — Viewer Jinja context + chrome

**Goal:** Viewer templates / FastAPI title / `static/style.css` /
`events.js` text use the personae.

**Changes:**
- `alice_viewer/main.py` loads personae at startup, exposes via
  `request.app.state.personae`.
- `templates/base.html` renders title as `{{ personae.agent.name }} Viewer`.
- `templates/memory.html`, `templates/narrative.html`, etc. swap
  hardcoded "Alice" for `{{ personae.agent.name }}`.
- `static/events.js:318` and similar JS strings are templated server-side
  if practical, or read personae JSON from a `<script>` tag the page
  embeds.
- `static/style.css:1` (a comment) is cosmetic — leave or strip; not
  load-bearing.

**Validation:** `tests/test_viewer_personae.py`:
- `test_base_template_renders_with_personae`
- `test_no_hardcoded_alice_in_rendered_html` — fixture personae has
  `agent.name = "TestAgent"`; render the index page; assert "TestAgent"
  appears and "Alice" does not.

**Exit criteria:** Viewer chrome is persona-aware.

---

### Phase 7 — Templated narrative summary prompts

**Goal:** `narrative.py`'s LLM-summary prompts (already migrated to
the prompts package in plan 04 Phase 4) have their hardcoded "Alice" /
"owner" replaced with `{{ agent.name }}` / `{{ user.name }}`.

**Changes:**
- Edit the templates in `src/alice_prompts/templates/viewer/`.
- No code changes — the loader's context defaults already include
  `agent` and `user`.

**Validation:**
- `tests/test_prompts.py::test_narrative_templates_use_personae_placeholders`
- Manual: render a narrative summary with a non-default
  `personae.agent.name`, confirm the summary uses it.

**Exit criteria:** Narrative prompts no longer mention "Alice" or
"owner" literally.

---

### Phase 8 — Default principal display + `recipient="owner"` retirement

**Goal:** The fallback CLI principal uses the user's actual name.
The `"owner"` recipient string is deprecated.

**Changes:**
- `principals.py:212-213` — defaults read from personae:
  - `fallback_cli_principal_id` = `personae.user.name.lower()`.
  - `fallback_cli_display_name` = `personae.user.name`.
- Register `"owner"` as an alias on the principal, with a
  `DeprecationWarning` emitted on lookup-via-alias.
- `tools/messaging.py:14` example text → `'"<user.name>"'`.

**Validation:**
- `tests/test_principals.py::test_default_cli_principal_uses_user_name`
- `tests/test_principals.py::test_owner_alias_resolves_with_warning`
- `tests/test_messaging.py::test_send_message_accepts_user_name_recipient`

**Exit criteria:** Owner alias works (back-compat) but emits a warning;
canonical recipient is the user's actual name.

---

### Phase 9 — Replace remaining literals + drop the `"owner"` alias

**Goal:** Final cleanup. Remove the back-compat alias from Phase 8.
Sweep for any "Alice" / "owner" literals missed.

**Changes:**
- `grep -rn '\bAlice\b' src/` → all remaining hits should be
  module/package names (`alice_speaking`, `alice_core`), docstrings
  (acceptable as project codename), or literals in tests with explicit
  fixtures.
- Same for `\bowner\b` and `\bOwner\b` — should be zero hits in the
  runtime (excluding watcher's GitHub-API URL paths like
  `repos/owner/repo`, which are real GitHub identifiers, not user
  references).
- Drop the `"owner"` alias from `principals.py`.

**Validation:** A new test in `tests/test_invariants.py`. **Note: a
naive `grep -rn '\bAlice\b' src/` won't work** — it trips on every
`from alice_speaking import …`, every `alice_core.*` reference, and
the docstrings the plan explicitly says are acceptable. Two
implementation paths:

- **AST-walking implementation:** Walk every `.py` file with
  `ast.parse`, look only at `ast.Constant` / `ast.Str` nodes (string
  literals — not identifiers, imports, or attribute accesses). Apply
  an allowlist for unavoidable cases (test fixtures with explicit
  `personae.agent.name = "Alice"`). Fail if any user-facing string
  literal contains the literal "Alice" or "owner" outside the
  allowlist. **~50 lines; commit to the effort.**
- **Advisory-grep fallback:** Skip the CI gate, run `grep` as a
  reviewer-aid only. Cheaper but the invariant decays — someone
  re-introduces a literal during a future change and nothing
  catches it.

**Recommendation:** AST-walking. The cross-cutting CI guards from
plan 00 already commit to similar effort; this is the same shape.
Build it once; reuse the literal-walker for similar checks.

Tests:
- `test_no_hardcoded_alice_in_user_facing_string_literals` —
  AST-walker version, with explicit allowlist for test fixtures.
- `test_no_hardcoded_owner_in_user_facing_string_literals` — same
  for owner.

**Exit criteria:** AST-walker passes against the codebase; the
back-compat alias is gone; the allowlist is small and reviewed.

---

## Tests

### Existing tests this plan must keep green

- `tests/test_kernel.py` — Phase 2 adds a field; existing assertions
  unaffected.
- `tests/test_principals.py` — Phase 8 changes defaults; update the
  test to use a fixture personae rather than hardcoded "owner".
- `tests/test_messaging.py` — Phase 8 changes `recipient` examples;
  test fixtures need updating.
- `tests/test_compaction.py` — the compaction prompt now contains
  `{{ user.name }}` rather than "the owner"; fixture renders with a
  test personae.
- All prompt / template / narrative / viewer tests from plan 04.

### New tests this plan introduces

- `tests/test_personae.py`:
  - `test_load_minimal_personae`
  - `test_load_full_personae`
  - `test_load_raises_on_missing_required_field`
  - `test_load_yaml_parse_error_clear_message`
  - `test_system_prompt_includes_agent_name`
  - `test_system_prompt_includes_user_name`
  - `test_system_prompt_includes_voice_rules_when_present`
  - `test_system_prompt_omits_optional_when_absent`

- `tests/test_kernel.py` extension (Phase 2):
  - `test_kernel_passes_append_system_prompt_to_options`
  - `test_kernel_omits_append_system_prompt_when_none`

- `tests/test_daemon.py` extension (Phase 3):
  - `test_kernel_spec_includes_system_prompt_from_personae`

- `tests/test_thinking_wake.py` extension (Phase 4):
  - `test_wake_kernel_spec_includes_personae`

- `tests/test_tools.py` (new, Phase 5):
  - `test_tool_descriptions_render_with_agent_name`
  - `test_tool_descriptions_render_with_user_name`
  - `test_no_tool_description_contains_literal_alice`

- `tests/test_viewer_personae.py` (Phase 6):
  - `test_base_template_renders_with_personae`
  - `test_no_hardcoded_alice_in_rendered_html`

- `tests/test_invariants.py` (Phase 9):
  - `test_no_hardcoded_alice_in_user_facing_code`
  - `test_no_hardcoded_owner_in_user_facing_code`

## Risks & non-goals

### Risks

- **The agent's behavior changes when she actually sees her
  identity in the system prompt.** Today she gets identity
  inconsistently (only when CLAUDE.md auto-loads + she chooses to
  read the persona files). Phase 3 makes it consistent. Watch wake
  logs for the first day post-deploy — she may suddenly start
  introducing herself, signing messages, or otherwise behaving
  more "in character" than before.

- **The "owner" alias breaks if any hardcoded path uses it without
  going through the principal lookup.** Audit before Phase 9: grep
  for `"owner"` literals in tool callbacks, the surface watcher,
  emergency handler. The watcher uses GitHub `owner/repo` paths —
  those are unrelated and stay.

- **YAML parse errors become a startup failure mode.** Today the
  agent has no `personae.yml`; she boots fine. After Phase 3, a
  malformed file blocks startup. The loader should produce a clear
  error message pointing at the file + line; the daemon should refuse
  to start rather than booting with degraded identity.

- **`personae.yml` lives in the mind repo (`data/alice-mind/`).** The
  mind is gitignored from this runtime repo but is itself a git repo
  with autopush. If a user accidentally commits a personae.yml with
  PII (real name, etc.) and pushes to a public mirror, that's an
  exposure. Document the privacy implications in the scaffold's
  README. **`bin/alice-init` must warn the user explicitly** before
  prompting for `user.name`: "this name will be written to
  `mind/personae.yml`, which lives in your mind git repo. If you've
  configured a public mirror for that repo, the name will be
  visible. Press y to continue, n to skip."

- **The compaction prompt currently has `the owner` baked in
  multiple places.** Each occurrence needs to render to the right
  pronoun/name; verify the rendered output flows naturally
  ("Jeremy's current state — mood, schedule, what they're working on").

### Non-goals

- **Not internationalizing prompts** — pronouns are a string field,
  not a full i18n system. If you set `pronouns: "they/them"`, the
  template uses it; templates don't try to grammar-check.

- **Not making `personae.yml` runtime-editable via a tool call** —
  the agent cannot rename herself or her user mid-session.
  Hand-edit + restart.

- **Not deleting the mind-scaffold's `IDENTITY.md` / `SOUL.md` /
  `USER.md`.** They remain as longer-form persona documents the agent
  can still read for richer context. The personae.yml is the
  *source of truth for runtime values*; the markdown files are for
  the agent's narrative self-understanding.

- **Not adding multi-user support** — `personae.yml` has one user
  block. Multiple humans (Jason + Katie) are still modeled via the
  principal book (`principals.yaml`). The personae's `user` is the
  *primary* human (the one whose Alice instance this is).

## Open questions

1. **One file or two?**
   `personae.yml` (one file with `agent` + `user` keys) is the
   recommendation. Splitting into `agent.yml` + `user.yml` adds two
   loaders and two failure modes for one ergonomic gain (separate
   git history). **Recommendation: one file.**

2. **Where in the mind repo does it live?**
   - `data/alice-mind/personae.yml` (top level, alongside SOUL.md /
     IDENTITY.md / USER.md). Visible. **Recommended.**
   - `data/alice-mind/config/personae.yml` (alongside
     `alice.config.json`, `principals.yaml`). Tucked away.
   - `data/alice-mind/.alice/personae.yml` (hidden config dir).

   Top level wins because the file is hand-edited by humans, not
   tools. Keep it discoverable.

3. **Should the principal book auto-derive from personae?**
   Today there's overlap — `principals.yaml` already has the user
   listed (often as "owner"). After this plan, both files have the
   user's identity. Two options:
   - Keep both; users edit personae.yml for identity, principals.yaml
     for transport-channel mappings (phone, Discord ID).
   - Personae auto-creates the primary principal; principals.yaml
     only adds extras (Katie, friend_carol).

   **Recommendation:** keep both files separate (different concerns —
   identity vs. addressing) but have personae auto-create the primary
   principal at startup if `principals.yaml` doesn't already have
   one matching the configured user.

4. **Should `voice.rules` go into the system prompt or stay as
   `SOUL.md`?**
   Both. Personae's `voice.rules` is a *short* set (3-5 lines). The
   long-form `SOUL.md` stays as the agent's narrative document.
   The system prompt includes the short rules; the agent reads
   `SOUL.md` if she wants to.

5. **Who creates `personae.yml` in a fresh install?**
   `bin/alice-init` should prompt for `agent.name` + `user.name` +
   `user.pronouns` interactively (like it already prompts for
   `SIGNAL_ACCOUNT`). Other fields default. The user can edit later.
   Prompts must include the PII warning above before capturing
   `user.name`.

6. **Where does `personae.py` live in `src/`?**
   Proposal: `alice_core/personae.py`. But `alice_core` is filling up
   (kernel, auth, events, session, sdk_compat, cortex_index, plus
   plan 06's `model_config.py`). Alternatives:
   - `alice_core/config/personae.py` — sub-package alongside auth.
   - New top-level `alice_config/` — separates configuration loaders
     from the kernel.
   This is the kind of question plan 08 (alice_core rationalization,
   per plan 00 §"Cross-plan handoffs") will ultimately settle.
   **Recommendation for this plan:** `alice_core/personae.py` for
   now; revisit when plan 08 is written.
