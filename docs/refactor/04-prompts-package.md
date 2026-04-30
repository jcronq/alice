# 04 — Prompts package

## Problem

Prompts live in seven different places, with no naming convention, no
templating system, and no index. Concretely:

- **`src/alice_speaking/compaction.py:29`** —
  `COMPACTION_PROMPT = (...)` — a multi-line Python string constant
  for the compaction summary prompt.
- **`src/alice_speaking/_sanity.py:31`** — inline `system_prompt="Reply
  verbatim to anything the user says. No preamble."` — a literal in
  a kernel call.
- **`src/alice_thinking/wake.py:50`** — `QUICK_PROMPT = "Reply exactly:
  QUICK-OK"`.
- **`src/alice_viewer/narrative.py:128, 353, 481`** — four full
  multi-paragraph LLM-summary prompts, each constructed in an f-string
  that interpolates the digest data and hardcodes "Alice" and
  "owner" by name.
- **`src/alice_speaking/render.py:47`** —
  `capability_prompt_fragment(channel)` — emits per-transport text
  describing what the channel can render. Built by string concat in
  Python.
- **`src/alice_speaking/daemon.py`** — per-handler prompt assembly
  for Signal / CLI / Discord / A2A / Surface / Emergency turns,
  each ~30-80 lines of f-string composition inside the handler.
- **`data/alice-mind/prompts/thinking-bootstrap.md`** — markdown file
  inlined into the wake prompt by `wake.py:_build_prompt`.
- **`data/alice-mind/inner/directive.md`** — markdown file inlined
  into the wake prompt by `wake.py:_build_prompt`.

There is **no single place** to review what Alice is told. There is
no naming convention (some are `*_PROMPT` constants, some are
functions, some are `.md` files). There is no template engine — every
prompt that needs interpolation does it with f-strings or `.format`,
inline at the call site. There is no way to change the prompt for one
hemisphere without finding the right file and editing the right
constant.

This couples prompt content to code structure. It also means plan 05
(personae) cannot work — substituting `{{agent.name}}` and
`{{user.name}}` into prompts requires somewhere to substitute *into*.

## Goal

After this plan:

- **Every prompt the runtime sends lives as a file under one canonical
  directory.** A reviewer can `ls` that directory and see the inventory.
- **Prompt files are named by purpose.** Naming convention:
  `<package>.<verb>.<surface>` (e.g. `speaking.compact`,
  `thinking.wake.bootstrap`, `viewer.narrative.daily`).
- **Prompts are templates** — Jinja2 (already a project dependency
  via the viewer) — with named placeholders for context.
- **One loader** — `alice_prompts.load(name, **context) -> str` — is
  the only sanctioned way to build a prompt at runtime.
- **Prompts are testable** — the loader is pure-function-shaped, so
  tests assert "given context X, the rendered prompt has substring Y."
- **No prompt strings remain in Python source** for prompts that get
  sent to the model. Internal log messages and exception strings stay
  as Python literals (they're not prompts).

## Design

### The package: `src/alice_prompts/`

A new top-level package, sibling to `alice_core`, `alice_speaking`,
etc. Layout:

```
src/alice_prompts/
├── __init__.py                # public API: load(), list(), reload()
├── loader.py                  # PromptLoader (Jinja2 + file resolution)
├── registry.py                # discovers prompt files at startup
├── templates/                 # default templates shipped with the runtime
│   ├── speaking/
│   │   ├── compact.md.j2
│   │   ├── capability.signal.md.j2
│   │   ├── capability.cli.md.j2
│   │   ├── capability.discord.md.j2
│   │   └── capability.a2a.md.j2
│   ├── thinking/
│   │   ├── quick.md.j2
│   │   ├── wake.timestamp_header.md.j2
│   │   ├── wake.active.md.j2          # was the bootstrap, mode-specific
│   │   ├── wake.sleep.consolidate.md.j2
│   │   ├── wake.sleep.downscale.md.j2
│   │   └── wake.sleep.recombine.md.j2
│   ├── viewer/
│   │   ├── narrative.window.md.j2
│   │   ├── narrative.daily.md.j2
│   │   └── narrative.weave.md.j2
│   └── meta/
│       └── sanity.md.j2
└── conftest_helpers.py         # test helpers: render_in_isolation()
```

### The loader

```
class PromptLoader:
    def __init__(self, search_paths: list[Path], context_defaults: dict): ...

    def load(self, name: str, **context) -> str:
        """Render the named template with the provided context.

        `name` is dot-separated (e.g. "speaking.compact"). The loader
        looks up the corresponding file in registered search paths in
        order, falling back to the runtime defaults under
        `src/alice_prompts/templates/`. The merged context is
        `self.context_defaults | context`.

        Raises PromptNotFound if no template matches.
        """

    def list(self) -> list[str]:
        """Return all known prompt names — for inventory tooling."""

    def reload(self) -> None:
        """Re-discover templates. Used by long-running processes
        (the daemon) to pick up edits without restart."""
```

### Search paths and override resolution

Templates resolve in priority order:

1. **`mind/.alice/prompts/`** — user-overrides (per-mind). Shipped as
   empty directory in the scaffold; users add files here to override
   defaults without touching the runtime package.
2. **`src/alice_prompts/templates/`** — the runtime defaults.

Override resolution is by name. If `mind/.alice/prompts/speaking/compact.md.j2`
exists, it wins. If it doesn't, the runtime default is used. This
mirrors how skills will resolve in plan 07.

### Context defaults

The loader is instantiated once at process start by `factory.py` (per
the speaking daemon's startup), with context defaults that include
the personae (after plan 05) — `agent`, `user` — so every template can
say `{{agent.name}}` without the caller passing it explicitly.

```
loader = PromptLoader(
    search_paths=[mind / ".alice/prompts", DEFAULT_TEMPLATES_DIR],
    context_defaults={
        "agent": personae.agent,
        "user": personae.user,
        "now": lambda: datetime.now(LOCAL_TZ),
    },
)
```

Per-call context (e.g. `digest`, `transport`, `wake_id`) is layered on
top of defaults at `load()` time.

### Template format

Plain Markdown with Jinja2 directives. File extension `.md.j2` makes
the format obvious to editors and tooling.

Example — `speaking/compact.md.j2`:

```
You are summarizing the conversation between {{ agent.name }} and
{{ user.name }} so far. Produce a structured handoff that the next
turn can read in place of the full history.

## Active threads
Open questions and pending tasks {{ user.name }} or {{ agent.name }}
has raised.

## {{ user.name }}'s current state
Mood, schedule, what they're working on.

[...]
```

### Discovery

`registry.py` walks the search paths once at loader init, indexing every
`*.md.j2` file by its dot-separated name (file-path-minus-extension with
slashes replaced by dots). Skill / template name collisions across search
paths resolve to the first match; the registry logs a debug line so
collisions are visible.

### Alternatives considered

- **YAML key/value file with prompts as values.** A single
  `prompts.yml` mapping names → strings. Rejected: multi-paragraph
  prompts in YAML are ugly (block literals + escaping); a single file
  becomes unmanageable past 5-6 prompts; per-prompt diffs in git get
  noisy.

- **Plain `.md` without Jinja.** Rejected: every existing prompt that
  uses interpolation needs templating; doing it ad-hoc in Python with
  `.format()` at the call site reproduces today's mess.

- **Skip Jinja, use Python `string.Template` (`$name` syntax).**
  Smaller dependency. But the viewer already pulls Jinja2, prompts
  benefit from `{% if %}` and `{% for %}` for conditional / repeated
  content (e.g. surface lists), and Jinja errors are clearer.

- **Keep prompts in code, just centralize the constants.** A single
  `prompts.py` with all the strings as Python constants. Cheaper, but
  loses (a) override resolution, (b) per-mind customization, (c) the
  ability for the agent to read her own prompts via a tool. Rejected.

- **Use the mind repo as the single source.** All prompts live in
  `data/alice-mind/.alice/prompts/`. No defaults shipped with the
  runtime. Rejected: a fresh mind would need to seed a dozen prompt
  files via `alice-init`, and runtime upgrades couldn't ship prompt
  fixes. The override-with-fallback pattern handles both cases.

## Phases

### Phase 1 — Skeleton: package, loader, one migrated prompt

**Goal:** `alice_prompts` exists, has a loader, has one template
(`thinking/quick.md.j2`), and `wake.py` reads it.

**Changes:**
- Create `src/alice_prompts/` package with `loader.py`, `registry.py`,
  `__init__.py`, `templates/`.
- Add `pyproject.toml` `[tool.hatch.build.targets.wheel].packages`
  entry: `"src/alice_prompts"`.
- Migrate `wake.py:50:QUICK_PROMPT` to `templates/thinking/quick.md.j2`
  (one line: `Reply exactly: QUICK-OK`). `wake.py` calls
  `prompts.load("thinking.quick")`.
- Loader instantiated module-level in `wake.py` for now (the daemon
  wires it through `factory.py` in a later phase).

**Validation:** `pytest tests/test_prompts.py`:
- `test_loader_finds_default_template`
- `test_loader_renders_with_context`
- `test_loader_raises_when_template_missing`
- `bin/alice-think --quick` against deployed worker still returns
  `QUICK-OK`.

**Exit criteria:** `pytest` green; one prompt migrated; loader works.

---

### Phase 2 — Migrate `compaction.py` and `_sanity.py`

**Goal:** Two more prompts move out of Python source.

**Changes:**
- `compaction.COMPACTION_PROMPT` → `templates/speaking/compact.md.j2`.
- `_sanity.py` inline → `templates/meta/sanity.md.j2`.
- Both call `prompts.load(...)` instead of using local constants.

**Validation:** `pytest tests/test_compaction.py` (existing); add
`tests/test_prompts.py::test_compact_template_renders_with_personae`
that asserts the compaction template includes `{{ agent.name }}` and
`{{ user.name }}` placeholders (the placeholders themselves, not yet
resolved — personae arrive in plan 05).

**Exit criteria:** Three prompts migrated; existing tests green.

---

### Phase 3 — Migrate per-transport capability fragments

**Goal:** `render.capability_prompt_fragment` moves to four
templates (`capability.signal/cli/discord/a2a.md.j2`).

**Changes:**
- Each transport's fragment becomes a template.
- `render.capability_prompt_fragment(channel)` becomes a one-liner:
  `prompts.load(f"speaking.capability.{channel.kind}")`.

**Validation:** Existing transport tests
(`test_a2a_transport`, `test_discord_transport`, `test_signal_*`)
keep passing. New test:
`test_capability_template_per_transport_exists` — for every transport
class, the corresponding template file exists.

**Exit criteria:** No `capability_prompt_fragment` string-concat code;
all four templates render with `transport.kind` context.

---

### Phase 4 — Migrate viewer narrative prompts

**Goal:** `narrative.py`'s four LLM-summary prompts move to templates.

**Changes:**
- `narrative.window.md.j2` (was the per-window summarizer).
- `narrative.daily.md.j2` (was the daily summary).
- `narrative.weave.md.j2` (was the multi-window weave).
- `narrative.py` calls `prompts.load(...)` for each.

**Validation:** No direct test for narrative output (it's an LLM call
against a mock), but the existing viewer narrative path keeps working.
Add `tests/test_prompts.py::test_narrative_templates_exist` and
`test_narrative_templates_have_required_placeholders` (each template
includes `{{ agent.name }}`, `{{ digest }}`, etc.).

**Exit criteria:** No multi-paragraph prompt strings in `narrative.py`.

---

### Phase 5 — Migrate the daemon's per-handler prompt assembly

**Goal:** Each handler in `_dispatch.py` (or `transports/<name>.py`,
post-plan-01) loads its prompt template instead of building it inline.

**Changes:**
- Per-handler prompt assembly (Signal turn prompt, CLI turn prompt,
  Discord turn prompt, A2A turn prompt, Surface turn prompt, Emergency
  turn prompt) extracted to templates under `speaking/turn.<kind>.md.j2`.
- Handlers call `prompts.load(f"speaking.turn.{event.kind}", batch=...,
  principal=..., transport=...)`.

**Validation:** Full daemon test suite (`tests/test_daemon.py`,
`tests/test_signal_batching.py`, `tests/test_signal_attachments.py`)
keeps passing. Plus a structural test:

- `test_every_event_kind_has_turn_template` — for every registered
  event kind (Signal, CLI, Discord, A2A, Surface, Emergency,
  whichever transports register), the corresponding
  `templates/speaking/turn.<kind>.md.j2` file exists. Without this
  test, a typo or new-transport-without-template surfaces as
  `PromptNotFound` only on first live event of that kind, not in CI.

**Exit criteria:** Daemon handlers contain no prompt-building string
ops; structural test catches missing templates at CI time.

---

### Phase 6 — Migrate `wake.py` thinking bootstrap and directive merge

**Goal:** Wake-time prompt assembly (timestamp header + bootstrap)
moves to templates. The mind-repo `prompts/thinking-bootstrap.md`
becomes a template that the runtime renders. **The directive stays
where it is** — see "directive vs. bootstrap boundary" below.

**Directive vs. bootstrap boundary.** Today's `thinking-bootstrap.md`
mashes together two distinct concerns with different lifecycles:

1. **Structural instructions** — the wake-cycle skeleton, mode
   selection, stage algorithms. Edited rarely, by the runtime
   maintainer. **Becomes the template.**
2. **`mind/inner/directive.md`** — Jason's standing operational
   orders, edited regularly. **Stays as a runtime-injected variable**,
   loaded fresh each wake and passed to the template via
   `{% include directive %}`.

The override path at `mind/.alice/prompts/thinking/wake.active.md.j2`
is for the bootstrap **template**. The directive is not a template;
it's data that the template includes.

**Changes:**
- `templates/thinking/wake.timestamp_header.md.j2`
- `templates/thinking/wake.active.md.j2` — new home for the structural
  parts of the previous bootstrap. The mind-repo override wins if
  present.
- `wake.py:_build_prompt` → `prompts.load("thinking.wake.active",
  directive=mind.read_directive(), ...)`. The loader passes the
  directive string into the template's render context; the template
  decides where to inject it (typically: at the top, under a
  `## Standing orders` heading).

**Validation:** `pytest tests/test_thinking_wake.py` from plan 03
(if shipped); manual: `bin/alice-think` produces a comparable wake
event log.

**Exit criteria:** No filesystem read of `prompts/thinking-bootstrap.md`
in `wake.py`; the loader handles it.

---

### Phase 7 — Per-mind override directory + scaffold

**Goal:** A new mind scaffolded by `alice-init` includes
`mind/.alice/prompts/` (empty), and instructions in the mind's CLAUDE.md
explain how to override.

**Changes:**
- `templates/mind-scaffold/.alice/prompts/.gitkeep`.
- `bin/alice-init` creates the directory.
- `templates/mind-scaffold/CLAUDE.md` documents the override pattern.
- `PromptLoader` adds `mind/.alice/prompts/` as the highest-priority
  search path.

**Validation:** Add `tests/test_prompts.py::test_override_wins_over_default`
— given a fixture mind with a custom `speaking/compact.md.j2`, the
loader returns the custom version.

**Exit criteria:** End-to-end override path works; documented.

---

### Phase 8 — Inventory tooling (optional but useful)

**Goal:** `bin/alice-prompts` lists, validates, and renders prompts.

**Changes:**
- `bin/alice-prompts list` — print all known prompt names.
- `bin/alice-prompts render <name>` — render with default context for
  inspection.
- `bin/alice-prompts validate` — check every template parses, every
  placeholder is documented.

**Validation:** Smoke tests in `tests/test_prompts_cli.py`.

**Exit criteria:** Inventory tooling works; useful for plan 05/07
review.

---

## Tests

### Existing tests this plan must keep green

- `tests/test_compaction.py` — Phase 2 must not change compaction
  behavior.
- `tests/test_daemon.py`, `tests/test_signal_batching.py`,
  `tests/test_signal_attachments.py` — Phase 5 must not change
  per-handler prompt content (only delivery mechanism).
- `tests/test_a2a_transport.py`, `tests/test_discord_transport.py` —
  Phase 3 + Phase 5.

### New tests this plan introduces

- `tests/test_prompts.py`:
  - `test_loader_finds_default_template` (Phase 1)
  - `test_loader_renders_with_context`
  - `test_loader_raises_when_template_missing`
  - `test_loader_lists_all_templates` — `loader.list()` returns the
    expected set.
  - `test_compact_template_renders_with_personae` (Phase 2)
  - `test_capability_template_per_transport_exists` (Phase 3)
  - `test_narrative_templates_have_required_placeholders` (Phase 4)
  - `test_override_wins_over_default` (Phase 7)
  - `test_loader_reload_picks_up_filesystem_changes` (Phase 7) —
    write a new template, call `reload()`, assert `list()` reflects it.

- `tests/test_prompts_cli.py` (Phase 8):
  - `test_alice_prompts_list_returns_zero`
  - `test_alice_prompts_validate_passes_on_clean_set`
  - `test_alice_prompts_validate_fails_on_missing_placeholder`

## Risks & non-goals

### Risks

- **Behavior change risk on multi-paragraph prompts.** Phase 4 (viewer
  narrative) and Phase 5 (per-handler) are migrating prompts that the
  agent has been seeing in a specific phrasing. The migration should
  preserve text byte-for-byte where possible. Diff the rendered output
  against the prior Python f-string output for at least one realistic
  context before merging.

- **Template caching.** Jinja2 caches compiled templates by default.
  In a long-running daemon, edits to `mind/.alice/prompts/` won't show
  up until `loader.reload()` is called. Either: (a) call `reload()` on
  config-mtime change, (b) document that prompt edits require a daemon
  restart. Recommendation: tie reload to the same path as
  `_maybe_reload_config` (config-mtime watcher).

- **`reload()` thread/concurrency safety.** The daemon's event loop
  may dispatch turns concurrently with a reload (config-mtime watcher
  fires on a different task). `PromptLoader.reload()` swaps the
  internal Jinja2 environment + template cache; do this atomically
  via a single rebind under an `asyncio.Lock`, or build the new
  environment off-thread and atomic-swap. Either way: a turn that
  starts mid-reload must complete with a consistent template set.
  Document the contract.

- **Sandbox escapes.** Jinja can be sandboxed; even so, prompts are
  templates we ship — user-supplied templates only enter via
  `mind/.alice/prompts/`, which is already under the user's control.
  No additional sandboxing needed.

- **Test fixture sprawl.** Tests that exercise prompts need fixture
  context (an `agent`, a `user`, a `digest`). Centralize in
  `conftest_helpers.py` to avoid copy-pasting context builders.

### Non-goals

- **Not changing the substance of any prompt** — the migration is a
  format change. Phase 4 (viewer narrative) is borderline; if the
  rendered text drifts, fix the template, not the test.
- **Not introducing a prompt versioning system** — git history serves.
- **Not making prompts runtime-editable via tool calls** — the agent
  cannot edit her own prompts. (She can write notes to thinking
  asking for prompt changes, and a human follows up.)
- **Not consolidating mind-repo prompts** (`thinking-bootstrap.md`,
  `inner/directive.md`) into the runtime. They stay where they are;
  the loader just reads them via the override path.

## Open questions

1. **Should `inner/directive.md` become a templated prompt or stay as
   plain text inlined into the wake?**
   It's plain text today. Recommendation: leave it as plain text —
   it's content, not a template. The wake template includes it
   verbatim via `{% include 'directive' %}` if present.

2. **Where does `alice_prompts` live in the dependency graph?**
   Top-level package alongside the others. Imports nothing from
   `alice_speaking` / `alice_thinking` / `alice_viewer`; they import
   from `alice_prompts`. Keep one-way.

3. **Should the loader be a singleton?**
   The daemon needs one shared loader (so reload is meaningful).
   Other entry points (the wake, the watcher one-shots) can have
   their own. Don't enforce; let `factory.py` create the right one
   per process.

4. **Naming convention: dots, slashes, or hyphens?**
   The proposal uses dots (`speaking.compact`). Filesystem layout uses
   slashes. Convention: name maps 1:1 with file path
   (`speaking.compact` ↔ `speaking/compact.md.j2`). Hyphens reserved
   for multi-word names within a segment (`speaking.surface-handoff`
   ↔ `speaking/surface-handoff.md.j2`).

5. **Do prompts under `mind/.alice/prompts/` need their own template
   syntax, or just markdown?**
   Same syntax as runtime defaults — both Jinja `.md.j2`. A user
   overriding without templating writes a `.md.j2` file with no
   directives; it works.
