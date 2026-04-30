# 07 — Skills as a first-class runtime concern

**Depends on plans 04 (prompts package) and 05 (personae).** Skills
will template their descriptions against the personae and load via
shared infrastructure. Plan 06 (backend selection) does **not** change
how skills are exposed — the Claude Agent SDK stays as the only kernel
implementation, so SDK-native skill auto-load remains the delivery
mechanism regardless of which backend (subscription / API / Bedrock)
the SDK is routing to.

## Problem

Skills today are **markdown files in a directory the runtime cannot
see**. They work, but only because Claude Code's CLI auto-discovers
`<cwd>/.claude/skills/<name>/SKILL.md` files and exposes them to the
agent. The Alice runtime — `alice_speaking`, `alice_thinking`,
`alice_viewer`, `alice_watchers` — does not know skills exist.

Concretely:

### What's there today

`data/alice-mind/.claude/skills/`:

```
.claude/skills/
├── README.md
├── log-meal/SKILL.md
├── log-workout/SKILL.md
├── update-weight/SKILL.md
└── cortex-memory/
    ├── SKILL.md
    ├── PATTERNS.md
    └── ops/
        ├── atomize.md
        ├── conflict.md
        ├── document.md
        ├── groom.md
        ├── link.md
        ├── promote.md
        ├── query.md
        └── reference.md
```

`templates/mind-scaffold/.claude/skills/`:

```
.claude/skills/
├── README.md
└── log-journal/SKILL.md
```

A skill is a markdown file with frontmatter:

```yaml
---
name: log-meal
description: Use when Jason reports eating something ("I ate X",
             "breakfast was X", "lunch: X"). Writes structured entries
             to Google Sheets, events.jsonl, and today's daily log.
             Don't ask follow-up questions unless a critical field is
             ambiguous.
---

# log-meal

Procedure: Jason tells me he ate something. Log it immediately —
don't rely on memory.

## Steps
...
```

The agent sees the description. When her in-flight reasoning matches
the description's trigger (e.g. user says "I ate eggs"), Claude Code
fires the skill — the SKILL.md body is loaded into context and the
agent executes the steps.

### What's missing or broken

1. **The runtime is blind to skills.** No skill registry, no
   inventory API, no way for `bin/alice` or the viewer to ask
   "what skills are loaded?" The list-of-skills lives in
   `.claude/skills/` and is discovered by Claude Code, not by
   anything we wrote.

2. **No telemetry.** When `log-meal` fires, nothing in the runtime
   logs `skill_invoked: log-meal`. The viewer cannot show "Alice
   used 4 skills today" or surface a skill that misfired. The
   "if a recurring task happens 3+ times, make it a skill" rule
   from `mind/CLAUDE.md` is unverifiable.

3. **No hemisphere scoping.** Speaking and thinking share the same
   `.claude/skills/` directory. Most skills (`log-meal`,
   `log-workout`, `update-weight`) are speaking-side action skills.
   `cortex-memory` is thinking-side. Today the agent has to
   **read prose in `mind/CLAUDE.md`** to know not to use `log-meal`
   directly during a daemon turn (the file says: *"in the Alice
   daemon context, these should produce a note for thinking rather
   than writing directly."*). That's context-switching by prose
   instruction — exactly the same anti-pattern as the
   stage-dispatch-by-prompt problem from plan 03.

4. **Description templating is hardcoded to one user.** `log-meal`'s
   description literally says *"Use when Jason reports eating
   something"*. Same for `log-workout`, `update-weight`. If
   `personae.user.name` is configured to "Eve", the skill won't fire
   correctly because the description asks the model to match against
   a name the user doesn't have.

5. **No testing harness.** A SKILL.md edit is a behavior change with
   no test. There's no `tests/test_skills.py` that asserts "given
   trigger phrase X, skill Y is the strongest match." Misfires only
   surface when a human notices.

6. **No override / inheritance.** Skills under
   `templates/mind-scaffold/.claude/skills/` are seeds for new minds.
   A skill the runtime ships defaults for (e.g. a hypothetical
   `summarize-day` skill) can't be overridden by the user's mind
   without copying the whole skill in. There's no "use the runtime
   default for this skill, override for that one" path.

7. **Skill discovery is hidden inside Claude Code's auto-loader.**
   The runtime can't enumerate skills, can't apply hemisphere scoping
   before they reach the model, can't render descriptions through
   personae context. Even with the SDK staying as the only kernel
   implementation (plan 06), the runtime still needs its own registry
   to do these things — and to keep working when the SDK's auto-load
   behavior changes.

8. **No `bin/alice` skill management.** The user can't ask "what
   skills do you have?" via `bin/alice skills list`. They'd have to
   ask the agent, who has to grep her own filesystem.

9. **The cortex-memory skill is a multi-doc structure** (SKILL.md +
   PATTERNS.md + 8 op files) that's effectively one composite skill.
   The runtime doesn't model the composition — Claude Code just sees
   one skill called `cortex-memory` whose body links to ops. A real
   skill registry could expose `cortex-memory.atomize`,
   `cortex-memory.conflict`, etc. as structured ops, with their own
   descriptions and triggers.

## Goal

After this plan:

- **The runtime owns the skill registry.** It enumerates skills at
  startup, applies hemisphere scoping, renders descriptions through
  the personae context (plan 05), and passes the resulting set to
  the provider (plan 06) — whether that's Claude Code or anything
  else.
- **Hemisphere scoping is declarative.** A SKILL.md frontmatter
  field `scope: speaking | thinking | both` (default `both` for
  back-compat). The registry filters by scope when building the
  per-hemisphere skill set.
- **Descriptions are templates.** A skill's `description:` field
  goes through the prompts package — `{{ user.name }}` becomes the
  configured user's name; `{{ agent.name }}` likewise.
- **Skills emit telemetry.** When a skill fires, the runtime emits
  a `skill_invoked` event the viewer renders. (Mechanism varies by
  provider — see Design.)
- **Skills are testable.** `tests/test_skills.py` runs a description-
  matching sanity check ("given trigger phrase X, the registered
  skill Y is in the candidate set") and a structural check (every
  SKILL.md has the required frontmatter).
- **Skills resolve from runtime defaults + mind overrides.** The
  runtime ships baseline skills under
  `templates/mind-scaffold/.claude/skills/`; a mind's
  `.claude/skills/` overrides individual skills by name; missing
  skills inherit from defaults. Same shape as the prompts package.
- **`bin/alice` exposes skill inventory.**
  `bin/alice skills list`, `bin/alice skills show <name>`,
  `bin/alice skills test "I ate eggs"` — operate on the registry.
- **Registry remains authoritative even if SDK auto-load changes.**
  The runtime's registry is the source of truth. The SDK auto-load
  remains the delivery mechanism, but the runtime can intervene
  (filter by scope, render descriptions through personae) before the
  SDK sees the on-disk skills — by writing the rendered output to
  the SDK's expected directory at startup, or by providing the SDK
  the registry's filtered set explicitly via the SDK's tool/skill
  APIs as those mature.

## Design

### `src/alice_skills/` package

A new top-level package. Layout:

```
src/alice_skills/
├── __init__.py                # public API: load_registry(), Skill, ScopeError
├── registry.py                # SkillRegistry class
├── skill.py                   # Skill dataclass + frontmatter parsing
├── discovery.py               # filesystem walking + override resolution
├── telemetry.py               # skill_invoked event emission helpers
└── matcher.py                 # description-matching for testing harness
```

### `Skill` dataclass

```
@dataclass(frozen=True)
class Skill:
    name: str
    description: str            # post-personae rendering
    description_template: str   # pre-render, for inspection
    scope: Literal["speaking", "thinking", "both"] = "both"
    body: str                   # full SKILL.md body (post-frontmatter)
    source_path: Path
    ops: tuple["Skill", ...] = ()  # nested ops (e.g. cortex-memory's ops/)

    @classmethod
    def parse(cls, path: Path) -> "Skill":
        """Parse SKILL.md, including frontmatter, walking ops/ if present."""
```

### Two kinds of file under `.claude/skills/<name>/`

The cortex-memory skill ships a top-level `SKILL.md` plus 8 op files
under `ops/` (`atomize.md`, `conflict.md`, etc.). The op files do
**not** have frontmatter today — they're loaded by the agent as
sub-procedure references when she invokes the parent skill.

`Skill.parse()` must distinguish:

- **Skills** — top-level `SKILL.md` files. Required frontmatter:
  `name`, `description`. Phase 1 handles these directly.
- **Ops** — files under a parent skill's `ops/` directory. Optional
  frontmatter; absent frontmatter is fine. Discriminated by location
  (under `ops/` of an existing skill).

The frontmatter discriminator is `kind:`:

```yaml
---
kind: skill | op       # default: skill if file is named SKILL.md, op if
                       # under <skill>/ops/, else error
name: ...              # required for skills, optional for ops
description: ...       # required for skills, optional for ops
---
```

Without this, Phase 1 tests pass on `log-meal/SKILL.md` but
`cortex-memory/ops/atomize.md` fails to load (no `name:`,
no `description:`).

### Frontmatter additions

Existing fields: `name`, `description`. New (optional, all backwards-
compatible):

```yaml
---
name: log-meal
description: Use when {{ user.name }} reports eating something
             ("I ate X", "breakfast was X"). Writes structured
             entries to Google Sheets, events.jsonl, and today's
             daily log.
scope: speaking                 # speaking | thinking | both (default: both)
fires_in_quiet_hours: false     # optional; defaults to true
emit_telemetry: true            # optional; defaults to true
ops:                            # optional; nested skills under ops/
  - atomize
  - conflict
  - document
---
```

### `SkillRegistry`

```
class SkillRegistry:
    def __init__(self, search_paths: list[Path], personae: Personae): ...

    def all(self) -> list[Skill]: ...

    def for_hemisphere(self, scope: str) -> list[Skill]:
        """Return skills whose scope is `scope` or `both`."""

    def find(self, name: str) -> Skill | None: ...

    def reload(self) -> None: ...
```

The registry is constructed once in `factory.py` and passed into the
provider (plan 06), which decides how to expose it.

### Override resolution

Same shape as prompts (plan 04 §"Search paths and override
resolution"). Search paths in priority order:

1. `mind/.claude/skills/` (existing user location).
2. `mind/.alice/skills/` (new — for the post-refactor world; both
   work, but this one signals "alice-aware skill, not just CC
   auto-load").
3. `templates/mind-scaffold/.claude/skills/` (runtime defaults
   shipped with this repo).

A skill named `cortex-memory` in path 1 wins over the same name in
path 3. Missing skills inherit from defaults.

### Hemisphere scoping in practice

- `factory.py` for the speaking daemon constructs the registry and
  filters: `registry.for_hemisphere("speaking")` → list of speaking
  skills.
- `wake.py` (thinking) does the same with `"thinking"`.
- The same SkillRegistry instance lives in both processes, but each
  asks for its slice.

A skill marked `scope: both` shows up in both. Today's existing
skills get default `both` so behavior is preserved on Phase 1; users
add explicit `scope:` over time.

### Telemetry

The SDK doesn't emit a "skill loaded" event the runtime can hook
directly. Instead, the speaking daemon's `BlockHandler` (the kernel
observer pattern from `alice_core.kernel`) intercepts the agent's
`Read` tool calls against `.claude/skills/<name>/SKILL.md`. When the
agent reads a SKILL.md, that's a strong signal she's about to invoke
that skill. The handler emits `skill_invoked: <name>`.

**Path source comes from the registry, not a glob.** After Phase 3
Path A lands, the per-hemisphere ephemeral skill directory is
`/state/worker/alice-skills/<hemisphere>/.claude/skills/`, not
`<mind>/.claude/skills/`. The BlockHandler must ask
`registry.is_skill_path(target_path)` to decide whether a `Read`
counts as a skill load — pattern-matching against a static
`<mind>/.claude/skills/` glob will silently miss every invocation
once the on-disk write step is in place. One line in Phase 5's
implementation; calling it out here so it isn't hardcoded.

**Two known limitations:**

1. **`Read(SKILL.md)` ≠ skill fired.** The agent investigating
   skills (when, e.g., asked "what skills do you have?") reads
   multiple SKILL.md files; each emits a `skill_invoked` event.
   That inflates counts and breaks the recurring-task heuristic
   (`mind/CLAUDE.md`'s "if a recurring task happens 3+ times, make
   a skill" rule becomes "verifiable" only in a trivial sense).

   Fix: the event carries an `intent: investigative | reactive`
   label. Heuristic: `reactive` when the same turn also produces
   a tool call matching a known skill effect (e.g. `log-meal` is
   reactive when the same turn writes to `events.jsonl`);
   otherwise `investigative`. The viewer counts only `reactive`
   in the "skill usage" view; both kinds are visible in raw logs.

2. **Subagent skill firings are invisible.** `log-meal` /
   `log-workout` / `update-weight` are typically fired inside a
   spawned subagent (via the SDK's `Task` tool). The parent
   daemon's `BlockHandler` doesn't see the subagent's tool-use
   blocks — it sees only the spawn + result.

   Fix: the daemon must hook the subagent event stream too. The
   SDK's subagent API exposes a per-task event channel; the daemon
   needs to consume it the same way it consumes the parent stream.
   This is non-trivial — call out as a Phase 5 sub-task with its
   own design check before merging.

Acceptable signal for trends and patterns; not for billing or audit.
Document both limitations in the viewer's skill-history view.

### Description templating

**Render lazily, not at registry construction.** Phase 1 stores
`description_template` (the raw frontmatter string). Phase 4 adds a
property:

```
class Skill:
    description_template: str

    def description(self, personae: Personae) -> str:
        """Render the description against the current personae.
        Memoized per (skill, personae_revision) — re-rendering only
        happens after personae reload."""
```

Why lazy:

- **Personae can reload at runtime** (config-mtime watcher per
  plan 05). Rendering at registry construction would freeze the
  description for the registry's lifetime; reload wouldn't propagate.
- **The semantic change between Phase 1 and Phase 4 is silent
  otherwise.** Phase 1's `Skill.description` is the raw template
  string; Phase 4's would be the rendered string. Code that reads
  `.description` works in both phases but means different things.
  Keeping `description_template` as the field and `description()`
  as the method makes the boundary explicit.
- Memoization keyed on `personae.revision` (incrementing on
  reload) avoids re-rendering on every read.

The existing `log-meal` description (literal "Jason") gets a
one-line edit to use `{{ user.name }}`.

### `bin/alice skills` subcommand

```
bin/alice skills list                # name + scope + description
bin/alice skills show <name>         # full body
bin/alice skills test "<trigger>"    # description matcher: which skills
                                     # would plausibly fire?
bin/alice skills validate            # structural check on every SKILL.md
```

The matcher (`bin/alice skills test`) uses a simple description-
similarity heuristic locally (no LLM call); useful for skill-author
sanity tests.

### Subagent skill availability

Skills available to the speaking daemon's main agent should also be
available to subagents (the `Task` tool's spawned children). The
SDK already passes the parent's skills to subagents by virtue of the
shared cwd; preserve that. Document explicitly in the SKILL.md
README that subagents inherit unless the skill says otherwise.

### Alternatives considered

- **Don't introduce a registry; let Claude Code keep doing skill
  discovery.** Cheapest. But none of the seven-or-so problems above
  go away. Specifically: no hemisphere scoping, no description
  templating, no provider portability.

- **Define skills in YAML/Python, not Markdown.** Skills are
  authored by humans (and edited by the agent herself when she
  writes new ones via "the 3-times rule"). Markdown is the right
  authoring shape. The registry parses markdown frontmatter.

- **One file per skill (no `ops/` subdirectories).** Simpler.
  But `cortex-memory`'s 8 ops are real composition — splitting
  them into 8 separate top-level skills loses the conceptual
  grouping. Keep ops as nested skills.

- **Skill triggering by code, not LLM-judged description match.**
  Some skills (`log-meal` based on regex over the user's message)
  could be code-triggered. But most skills require LLM judgment
  to identify the trigger. Stay with description-match; consider
  code-triggered skills as a future extension (a `Skill.matcher:
  Optional[Callable[[str], bool]]` field).

- **Make every skill's body live in the prompts package.**
  Tempting (one place for all model-facing text). But skills are
  procedures (often with shell commands, file paths, etc.) — they
  read like ops manuals more than like prompts. Different lifecycle,
  different authoring audience. Keep them separate.

## Phases

### Phase 1 — Skill loader + registry, no consumers

**Goal:** Parse existing SKILL.md files into `Skill` objects.
Nothing uses the registry yet.

**Changes:**
- `src/alice_skills/` package with `skill.py`, `registry.py`,
  `discovery.py`, `__init__.py`.
- `Skill.parse()` reads frontmatter (already YAML), validates
  required fields, captures body.
- `SkillRegistry` walks the search paths, resolves overrides,
  exposes `all()` / `for_hemisphere()` / `find()`.
- `pyproject.toml`: `"src/alice_skills"` in
  `[tool.hatch.build.targets.wheel].packages`.

**Validation:** `tests/test_skills.py`:
- `test_parse_skill_with_minimal_frontmatter`
- `test_parse_skill_with_full_frontmatter`
- `test_parse_skill_with_ops_subdirectory` — `cortex-memory` style.
- `test_parse_skill_raises_on_missing_name`
- `test_parse_skill_raises_on_missing_description`
- `test_registry_resolves_override_from_mind_over_default`
- `test_registry_for_hemisphere_filters_by_scope`
- `test_registry_for_hemisphere_includes_both_scope`

**Exit criteria:** Registry parses every existing SKILL.md without
error; tests green.

---

### Phase 2 — Inventory CLI: `bin/alice skills list / show / validate`

**Goal:** Manual inspection works. No runtime behavior change.

**Changes:**
- `bin/alice` extended with a `skills` subcommand (or new
  `bin/alice-skills`).
- `list` prints name + scope + description.
- `show <name>` prints the body.
- `validate` runs structural checks: every SKILL.md parses, every
  description has a closing sentence, every `scope:` is valid.

**Validation:** `tests/test_skills_cli.py`:
- `test_alice_skills_list_outputs_known_skills`
- `test_alice_skills_show_returns_body`
- `test_alice_skills_validate_passes_on_clean_set`
- `test_alice_skills_validate_fails_on_bad_frontmatter`

**Exit criteria:** CLI works; validate command catches bad SKILL.md
files.

---

### Phase 3 — Add `scope:` frontmatter; default both **(behavior change at end)**

**Goal:** Frontmatter accepts `scope:`. Existing skills are tagged
explicitly. The runtime not only filters the registry by scope but
**actually delivers a scope-filtered skill set to the SDK** — see
"Enforcement mechanism" below.

**Changes:**
- `Skill.parse()` reads `scope:` (defaults to `"both"`).
- Edit existing skills:
  - `data/alice-mind/.claude/skills/log-meal/SKILL.md` →
    `scope: speaking`.
  - `data/alice-mind/.claude/skills/log-workout/SKILL.md` →
    `scope: speaking`.
  - `data/alice-mind/.claude/skills/update-weight/SKILL.md` →
    `scope: speaking`.
  - `data/alice-mind/.claude/skills/cortex-memory/SKILL.md` →
    `scope: thinking`.
  - `templates/mind-scaffold/.claude/skills/log-journal/SKILL.md` →
    `scope: speaking`.
- Enforcement mechanism (see below) — at startup, write the rendered
  scope-filtered skills into a per-hemisphere directory the SDK reads.

**Enforcement mechanism — the registry filter is not enough on its
own.** `SkillRegistry.for_hemisphere()` produces a Python list of
`Skill` objects, but the Claude Agent SDK auto-discovers skills by
walking `<cwd>/.claude/skills/` directly. The registry's filter has
no effect on what the SDK delivers to the LLM unless we control what
the SDK sees on disk.

Three viable enforcement paths:

- **Path A — Per-hemisphere skill directory at startup.** At process
  start, the runtime writes the rendered, scope-filtered skill files
  into an ephemeral directory (e.g.
  `/state/worker/alice-skills/speaking/.claude/skills/`). The SDK is
  configured to use that directory as cwd or skill root. Speaking
  reads `speaking/`; thinking reads `thinking/`. **Recommended.**
  Speaking and thinking already run as separate processes; pointing
  each at a different cwd is mechanically straightforward.

- **Path B — SDK skill filter API.** Wait for the SDK to expose
  per-turn skill filtering as a first-class API. Doesn't exist yet;
  not a defensible target.

- **Path C — Single shared `.alice/skills/` directory written at
  startup.** Hybrid of A and the existing `.claude/skills/` —
  introduces a new directory the SDK is configured to read instead
  of `.claude/skills/`. Lets `claude` CLI invoked manually still hit
  the unfiltered `.claude/skills/`, while alice-runtime processes
  see the filtered set.

**Phase 3 ships Path A.** Requires:

- A startup step (an `EnforceSkillScopeStartup` `StartupSource` from
  plan 01) that, before the SDK initializes, writes the filtered
  rendered skills to the hemisphere-specific directory.
- The hemisphere's kernel cwd points at that directory's parent.
- Cleanup on shutdown is optional (ephemeral, regenerated next run).

**The behavior change** is at deploy time: thinking literally no
longer has `log-meal` in its `.claude/skills/`; speaking literally
no longer has `cortex-memory`. This makes the existing prose
instruction in `mind/CLAUDE.md` ("in the Alice daemon context these
should produce a note for thinking rather than writing directly")
**enforceable by file system**, not just registry filter.

**Phase 3 PR must not merge claiming "enforceable by code" until the
on-disk write step is implemented.** Merging earlier (Path B
deferred) would ship a Python-level filter that doesn't actually
restrict what the agent sees — the exact aspirational-interface
pattern plan 00 §"Root cause" calls out.

**Validation:**
- `tests/test_skills.py::test_existing_skills_have_correct_scope`
— spot-check that the existing skills resolve to expected scopes.
- Manual: deploy, ask the speaking agent for a meal log →
  `log-meal` skill fires (still in scope). Ask the thinking
  agent → it doesn't. Watch wake/turn logs.

**Exit criteria:** Each hemisphere only sees its in-scope skills;
behavior matches the documented hemisphere boundary.

---

### Phase 4 — Description templating against personae **(depends on plan 05)**

**Goal:** Skill descriptions render against the personae. "Jason"
in `log-meal`'s description becomes `{{ user.name }}`.

**Changes:**
- `Skill.description` becomes the *rendered* string;
  `Skill.description_template` is the raw string.
- `SkillRegistry.__init__(personae=...)` — render at registry
  construction.
- Edit existing skills to use `{{ user.name }}` where the literal
  username appears.

**Validation:**
- `tests/test_skills.py::test_description_renders_user_name`
- `tests/test_skills.py::test_description_renders_agent_name`
- `tests/test_skills.py::test_description_unchanged_when_no_template`
- Manual: rename `personae.user.name` to "Eve", deploy, run a meal
  log → skill description now references "Eve".

**Exit criteria:** Skill descriptions are persona-aware.

---

### Phase 5 — Telemetry on skill use

**Goal:** When a skill fires, the runtime emits `skill_invoked`.
Viewer renders it.

**Changes:**
- Provider adapter (Claude Agent SDK) intercepts `Read` tool calls
  whose target matches a skill's `source_path` and emits
  `skill_invoked: <skill name>`.
- `alice_core.events` documents the new event type.
- Viewer's `aggregators.py` adds a `skill_invocations` view.
- `bin/alice skills history --since=24h` queries the event log.

**Validation:**
- `tests/test_skills_telemetry.py::test_read_of_skill_md_emits_invocation_event`
- `tests/test_skills_telemetry.py::test_read_of_non_skill_does_not_emit`
- Manual: ask the agent to log a meal, confirm the viewer's
  timeline shows a `skill_invoked: log-meal` row.

**Exit criteria:** Viewer surfaces skill usage; the recurring-task
heuristic from `mind/CLAUDE.md` becomes verifiable.

---

### Phase 6 — Override resolution from runtime defaults

**Goal:** Skills can be shipped with the runtime as defaults; minds
override individual skills.

**Changes:**
- Move `templates/mind-scaffold/.claude/skills/log-journal/SKILL.md`
  to ship as a default — it's not part of the scaffold seed but a
  runtime-default skill.
- (Or keep `templates/mind-scaffold` as the seed and introduce a
  new `src/alice_skills/defaults/` directory — see open questions.)
- `SkillRegistry` registers the runtime-default search path with
  the lowest priority.
- The mind's `.claude/skills/` continues to override.

**Validation:**
- `tests/test_skills.py::test_override_path_wins_over_default`
- `tests/test_skills.py::test_default_skills_loaded_when_mind_absent`
- Manual: delete the user's `log-journal/SKILL.md` from the mind;
  the registry still loads the runtime default.

**Exit criteria:** Layered resolution works.

---

### Phase 7 — Skill testing harness

**Goal:** `tests/skills/` exists; given trigger phrases, asserts the
right skill is in the matcher's candidate set.

**Changes:**
- `tests/skills/cases.yml` — list of (trigger phrase → expected
  skill name) pairs.
- `tests/skills/test_matcher_local.py` — fast offline lane. Runs
  each case through `matcher.candidate_skills(trigger, registry)`
  using simple description tokenization + score (TF-IDF–ish, no
  LLM). Catches obvious regressions in skill descriptions.
- `tests/skills/test_matcher_llm.py` — gated `@pytest.mark.skill_llm`
  lane. Runs each case against a real Haiku call via the
  `claude_agent_sdk` (or whichever provider is configured), asks
  the model to pick the most-fitting skill from the registry's
  description set, asserts the answer matches `expects`. Paid; only
  runs when explicitly invoked.

**Why two lanes.** Production skill matching is LLM-driven
(description-based fuzzy semantic match). A TF-IDF tokenizer tests
a different system than production. A description edit that
preserves keywords but breaks the LLM's read of intent passes the
local lane and fails in production. The LLM lane closes that gap
but is paid; gate it behind a marker so default `pytest` skips it.

**Validation:** `pytest tests/skills/test_matcher_local.py`
(default); `pytest -m skill_llm tests/skills/test_matcher_llm.py`
(periodic / pre-deploy).

**Exit criteria:** Local lane catches obvious regressions in CI.
LLM lane runs at least weekly (or before any skill-system PR
merges) and fails loudly if production routing diverges.

## Tests

### Existing tests this plan must keep green

(There are none for skills today — they're untested.)

- The full `pytest` suite must keep green through every phase.
- Manual: existing skill behaviors (`log-meal`, `log-workout`,
  `update-weight`, `cortex-memory`) must continue to fire correctly
  through Phase 3 (when scoping kicks in) and Phase 4 (when
  templating kicks in).

### New tests this plan introduces

- `tests/test_skills.py` (Phase 1):
  - `test_parse_skill_with_minimal_frontmatter`
  - `test_parse_skill_with_full_frontmatter`
  - `test_parse_skill_with_ops_subdirectory`
  - `test_parse_skill_raises_on_missing_name`
  - `test_parse_skill_raises_on_missing_description`
  - `test_registry_resolves_override_from_mind_over_default`
  - `test_registry_for_hemisphere_filters_by_scope`
  - `test_registry_for_hemisphere_includes_both_scope`
  - `test_existing_skills_have_correct_scope` (Phase 3)
  - `test_description_renders_user_name` (Phase 4)
  - `test_description_renders_agent_name`
  - `test_description_unchanged_when_no_template`
  - `test_override_path_wins_over_default` (Phase 6)
  - `test_default_skills_loaded_when_mind_absent`

- `tests/test_skills_cli.py` (Phase 2):
  - `test_alice_skills_list_outputs_known_skills`
  - `test_alice_skills_show_returns_body`
  - `test_alice_skills_validate_passes_on_clean_set`
  - `test_alice_skills_validate_fails_on_bad_frontmatter`

- `tests/test_skills_telemetry.py` (Phase 5):
  - `test_read_of_skill_md_emits_invocation_event`
  - `test_read_of_non_skill_does_not_emit`
  - `test_invocation_event_includes_skill_name_and_correlation_id`
  - `test_handler_uses_registry_is_skill_path_not_static_glob` —
    after Phase 3 Path A, the ephemeral per-hemisphere skill dir is
    the right source.
  - `test_subagent_skill_invocation_emits_event` —
    `@pytest.mark.xfail(reason="Subagent observer not yet wired —
    see Phase 5 sub-task on subagent telemetry")`. Stays red until
    the SDK exposes subagent observers or this plan documents the
    gap explicitly. A concrete xfail test is a sharper forcing
    function than a prose-level "design check before merging."

- `tests/skills/test_matcher.py` (Phase 7):
  - One test per case in `tests/skills/cases.yml`.
  - `test_matcher_handles_unknown_trigger` — empty candidate set,
    no exception.

## Risks & non-goals

### Risks

- **Phase 3 changes which skills each hemisphere sees.** This is the
  enforceable hemisphere boundary the user wanted, but if a skill
  was misclassified the agent loses access. **"User is around to
  notice" is hope, not mitigation.** Real mitigation: a `--dry-run`
  mode that diffs pre/post-Phase-3 hemisphere visibility. Add to
  Phase 3:
  - `bin/alice skills diff --hemisphere=speaking` — show which
    skills are present in `.claude/skills/` today vs. which would
    be present after Phase 3's filter. Run before merging.
  - `bin/alice skills diff --hemisphere=thinking` — same.
  Plus the safety net: any `scope:` not present defaults to `both`
  (so missing-frontmatter skills don't disappear from either
  hemisphere).

- **Phase 4 (templating) introduces Jinja2 to skill descriptions.**
  A SKILL.md with literal `{{` in its description (unlikely but
  possible) will fail to render. Add a structural-check rule in
  `bin/alice skills validate` that catches unescaped Jinja in
  descriptions that don't intend it.

- **Phase 5 telemetry is heuristic.** A `Read` of `SKILL.md`
  doesn't guarantee the skill fires (the agent could have decided
  not to follow it). Document: `skill_invoked` means "skill body
  was loaded into context"; it does not mean "the skill's effects
  ran." Fine for trend visibility; not fine for billing or auditing.

- **Skill ops nested under `cortex-memory/ops/` are not auto-
  discovered by Claude Code today** as separate skills — they're
  only loaded when the agent reads them mid-procedure. Phase 1's
  registry models them as `ops:` for inventory; Phase 5's telemetry
  doesn't fire on op reads (the `cortex-memory` invocation already
  fired). Document the model.

### Non-goals

- **Not making skills runtime-editable by the agent through a tool
  call.** She can write a new `SKILL.md` to disk via her existing
  `Write` tool; that's the only path. (And it's caught at registry
  reload.)

- **Not implementing a marketplace or skill-sharing mechanism.**
  Out of scope. Skills shared between minds happen via git, copy-
  paste, or by including in the runtime defaults.

- **Not unifying skills with prompts** — they have different
  authoring audiences and lifecycles. The prompts package is
  what the model sees in the system prompt + per-turn prompts;
  skills are procedural ops the agent invokes mid-turn.

- **Not adding skill versioning beyond git.** Skills are markdown
  in a git repo. Version is whatever's at HEAD.

- **Not introducing skill chaining / composition primitives** beyond
  the existing nested-ops pattern. Composition happens via prose
  inside SKILL.md (one skill saying "after step 3, invoke
  `update-weight`").

## Open questions

1. **Where do runtime-default skills live?**
   - `templates/mind-scaffold/.claude/skills/` — co-located with
     the rest of the scaffold; matches existing convention. **But:**
     scaffold files seed a *new* mind; defaults that should override-
     into existing minds need a different home.
   - `src/alice_skills/defaults/` — clearly a runtime concern, separate
     from scaffolding. **Recommended.**
   - Both — scaffold has its seed, runtime has its defaults. Probably
     overkill.

2. **Should the `scope: both` default change to `speaking` over time?**
   Today's existing skills are mostly speaking-side. Defaulting to
   `speaking` would catch new skills that forgot to declare scope.
   But it'd silently exclude legitimate `both`-scope skills.
   **Recommendation: keep `both` as the default; add a lint warning
   in `bin/alice skills validate` for skills with `scope:` unset
   (encourage explicit declaration).**

3. **Do nested ops (e.g. `cortex-memory/ops/`) need their own
   `scope:`?**
   Today they inherit from the parent. A `cortex-memory.atomize` op
   could plausibly fire from speaking (the user asks her to atomize
   a note while talking) — different scope than the parent. Add an
   ops-level `scope:` field; default to parent's. Out of scope for
   this plan; revisit if it bites.

4. **Should `bin/alice skills test` use an LLM-based matcher
   (more accurate) or stay TF-IDF (offline, fast)?**
   Offline first. An LLM-based matcher could be a Phase 9 add.

5. **Where do skill telemetry events sit relative to `kernel`
   events?**
   Same channel. `alice_core.events.EventEmitter` already handles
   the structured event log. Add a `skill_invoked` event type with
   `skill_name`, `correlation_id`, optional `match_confidence`.

6. **Migration: do we move `data/alice-mind/.claude/skills/` to
   `data/alice-mind/.alice/skills/`, or leave them where they are?**
   Leave existing skills where they are; Claude Code's auto-loader
   still wants `.claude/skills/`. The new `.alice/skills/` path is
   for cases where a mind wants to declare alice-aware skills that
   shouldn't be auto-loaded by raw Claude Code (e.g.
   thinking-only skills that would confuse a user running
   `claude` directly in the mind dir).

7. **Should there be a built-in `register-skill` skill** that, when
   the agent is asked to make a new skill, walks her through the
   3-times rule, validates the description, etc.?
   Meta-skill; probably yes. Not part of this plan; ships as a
   default skill once the registry is in place.

8. **Should this plan split into 07a / 07b / 07c?**
   The plan covers seven concerns: registry parsing, scope
   enforcement, description templating, telemetry, override
   resolution, provider exposure, testing. Some can ship
   independently:
   - 07a (Phases 1–3): registry, scope filter, on-disk enforcement.
   - 07b (Phases 4–5): description templating + telemetry.
     Depends on plan 05; can slip independently of 07a.
   - 07c (Phases 6–7): runtime defaults + testing harness.
   **Recommendation:** keep as one plan for now; if Phase 3's SDK
   gap (Path A implementation) stalls, split off 07b and 07c so
   the templating/telemetry work doesn't wait for scope
   enforcement to land.

9. **Override resolution priority — runtime defaults must win
   updates against frozen scaffold copies.** The plan's current
   layered resolution puts runtime defaults at priority 3 (or via
   the scaffold copy, which is priority 1 once `alice-init` runs).
   That replays the same anti-pattern as `templates/mind-scaffold/HEMISPHERES.md`:
   user scaffolds once, freezes a copy, and the runtime can never
   push updates. **Phase 6 must commit to `src/alice_skills/defaults/`
   as the source of runtime defaults**, never `templates/mind-scaffold/.claude/skills/`.
   Mind-scaffold may seed user-editable starter skills (e.g.
   `log-journal`); those are for the user to keep or delete.
   Runtime-default skills (e.g. a future `summarize-day`) ship from
   the runtime package and update with the runtime.
