# 08 — alice_core organization

**Recommended position in the dependency graph:** ships **before**
plan 05 (personae) and plan 06 (backend selection) merge — so their
new files land directly into the right structure. Parallel with plan 03.

This plan is the cheapest, highest-clarity item in the corpus. Source:
Alice's own sketch in
`cortex-memory/research/2026-04-29-refactor-plan-08-alice-core-organization-sketch.md`,
informed by reading the actual `src/alice_core/` contents (not just
the plan documents).

## Problem

`src/alice_core/` is the runtime kernel package — it ships with the
docstring:

> "Owns the pieces that don't care which hemisphere is running [...]
> Neither a daemon nor an entry point; pure library."

That claim is the same kind of aspirational-interface-never-enforced
pattern plan 00 §"Root cause" identified in seven other places. It
doesn't actually hold:

- `cortex_index/build_index.py` (407 lines) is a runnable CLI for
  walking and indexing the cortex-memory vault. It cares which mind
  is running. Not hemisphere-agnostic; not pure library; entry point.
- `cortex_index/yaml_lite.py` (205 lines) is a stdlib-only YAML
  frontmatter parser used only by the indexer. Tied to the same
  vault tool.

Today's `alice_core/`:

```
src/alice_core/
├── __init__.py            (package docstring)
├── auth.py                177 lines — env-var auth resolver
├── config.py                1 line  — "Stub — may be fleshed out"
├── cortex_index/          624 lines total — vault SQLite/FTS5 indexer
│   ├── build_index.py     407
│   ├── yaml_lite.py       205
│   └── README.md
├── events.py               84 lines — observability primitives
├── kernel.py              357 lines — AgentKernel, KernelSpec, BlockHandler
├── sdk_compat.py           63 lines — SDK-version-quirk shims
└── session.py             110 lines — session.json persistence
```

After plans 05 + 06 land, three more files arrive:

- `personae.py` (plan 05) — agent + user identity loaders.
- `model_config.py` (plan 06) — backend selection config loader.
- (kernel.py grows fields per plans 03/05.)

Reading the resulting directory listing, a new contributor can't tell
what's runtime, what's observability, what's config, and what's a
vault tool. The package layout no longer communicates intent — the
exact thing this whole refactor is supposed to fix at the
`alice_speaking/` level (plan 02). It's hypocritical not to also fix
it at the `alice_core/` level.

## Goal

After this plan:

- **`alice_core/` contains only kernel-level concerns.** The vault
  indexer leaves; configuration loaders cluster.
- **`cortex_index/` becomes its own top-level package** (`alice_indexer/`),
  reflecting that it's a vault tool consumed by the cue runner /
  hygiene scripts, not by the kernel.
- **Three configuration-shaped files** (`auth.py`, `model_config.py`,
  `personae.py`) live under `alice_core/config/`. Same shape (read
  source → typed frozen dataclass → loader function); same parent.
- **The package's docstring is true.** alice_core imports from the
  Claude Agent SDK, Python stdlib, and itself. Nothing else.
- **A CI guard enforces the dependency direction**, so the day someone
  inverts it (e.g. plan 05's KernelSpec dragging in alice_speaking),
  CI fails before merge.

## Design

### Final layout

```
src/alice_core/
├── __init__.py
├── kernel.py            — AgentKernel + KernelSpec + BlockHandler
├── sdk_compat.py        — SDK-version shims
├── session.py           — session.json read/write
├── events.py            — EventEmitter + EventLogger
└── config/
    ├── __init__.py      — public re-exports: load_auth(), load_model(), load_personae()
    ├── auth.py          — moved from alice_core/auth.py
    ├── model.py         — was model_config.py (plan 06; renamed for symmetry)
    └── personae.py      — was personae.py (plan 05)

src/alice_indexer/         (NEW — was alice_core/cortex_index/)
├── __init__.py
├── build_index.py
├── yaml_lite.py
└── README.md
```

Six things at the alice_core top level: four runtime modules + one
config subpackage + the package init. Reads cleanly.

### Concerns and where they go

1. **Runtime core.** `kernel.py`, `sdk_compat.py`. Drives the SDK.
   Stays at top level.
2. **Observability.** `events.py`. EventEmitter protocol, structured
   event logger. Stays.
3. **Persistence (kernel-level).** `session.py`. PersistedSession
   atomic file ops. Stays.
4. **Configuration.** `auth.py`, `model.py`, `personae.py`. All
   read-source → typed-dataclass → loader. Move to `config/`.
5. **Vault tooling.** `cortex_index/`. Move out entirely.

### Dependency rule (enforced by CI)

`alice_core/` imports only from:

- The Claude Agent SDK.
- Python stdlib.
- Other modules within `alice_core/`.

`alice_core/` must NOT import from:

- `alice_speaking`, `alice_thinking`, `alice_viewer`, `alice_watchers`
- `alice_prompts` (plan 04 — depends on alice_core, not the reverse)
- `alice_skills` (plan 07 — same direction)
- `alice_indexer` (depends on alice_core for `events`, not the reverse)

This direction is conventional ("everything depends on the kernel;
the kernel depends on nothing above") but not enforced today. The
new arrivals from plans 05/07 risk inversion: a `KernelSpec` field
typed as `Personae` is fine if `Personae` is in alice_core; not fine
if it's anywhere else. The CI guard catches accidental inversion
on the day it lands.

### Alternatives considered

- **Don't reorganize alice_core.** Cheapest. But it leaves the
  hypocrisy intact — plans 02 and 03 fix layout in `alice_speaking/`
  and `alice_thinking/` while the kernel package stays a heap.

- **Promote `config/` to a top-level `alice_config/` package.**
  Cleaner separation, but config needs `EventEmitter` from
  alice_core for emitting reload events; promoting `config/`
  out introduces cyclic-dep risk. Keep nested for now;
  re-evaluate if `config/` grows past ~5 modules.

- **Split `kernel.py` into `kernel.py` + `spec.py` + `handler.py`.**
  357 lines is large but coherent. Splitting is reasonable but
  doesn't move the package-organization needle. Defer.

- **Move `session.py` into `state/` subpackage.** Premature — it's
  the only kernel-state file. Revisit if a second arrives.

- **Keep `cortex_index/` under alice_core but rename the package
  to `alice_runtime`.** Overcorrection — alice_core is the right
  *concept*, cortex_index just doesn't fit it. Move the misfit.

## Phases

### Phase 1 — Extract `cortex_index` to `alice_indexer`

**Goal:** `src/alice_indexer/` exists; `src/alice_core/cortex_index/`
is gone.

**Changes:**
- `git mv src/alice_core/cortex_index src/alice_indexer`
- Update `pyproject.toml` `[tool.hatch.build.targets.wheel].packages`:
  add `"src/alice_indexer"`.
- Grep for `from alice_core.cortex_index` / `import
  alice_core.cortex_index` across `src/`, `tests/`, `bin/`,
  `data/alice-mind/.claude/`, `data/alice-tools/`. Update to
  `from alice_indexer import ...`.
- **No back-compat shim** — the package was internal, so external
  callers (if any) get a clear `ImportError`. Document in the
  Phase 1 PR description in case a manual fix is needed.

**Validation:** `pytest`. Manual:
`python3 src/alice_indexer/build_index.py --check && python3
src/alice_indexer/build_index.py` builds the index against
`~/alice-mind/cortex-memory/`.

**Exit criteria:** alice_core no longer contains a vault-tool
subpackage; the indexer is independently buildable.

---

### Phase 2 — Introduce `alice_core/config/` subpackage

**Goal:** Configuration-shaped files cluster under `config/`. The
arrival of plan 05's `personae.py` and plan 06's `model.py` lands
into this structure directly.

**Sequencing:** This phase ships **before** plan 05 and plan 06 merge.
The structure is the prerequisite, not the consequence. Plan 05/06's
new files have a one-line path edit
(`src/alice_core/personae.py` → `src/alice_core/config/personae.py`)
and otherwise are unchanged.

**Changes:**
- `mkdir src/alice_core/config/`
- `touch src/alice_core/config/__init__.py`
- `git mv src/alice_core/auth.py src/alice_core/config/auth.py`
- `src/alice_core/config/__init__.py` re-exports the public surface:
  ```
  from .auth import (
      AuthEnv, AuthMode, ensure_auth_env, ensure_token,
      find_auth_env, find_token,
  )
  ```
- `src/alice_core/auth.py` becomes a one-line shim:
  ```
  # Deprecated: import from alice_core.config.auth.
  from .config.auth import *   # noqa: F401,F403
  ```
  Per the cross-cutting "shims live for one plan" rule, drops in
  Phase 3.
- Delete `src/alice_core/config.py` (the empty stub).

**Validation:** `pytest`. Plus `python -c "from alice_core.auth import
ensure_auth_env; from alice_core.config.auth import ensure_auth_env"`
— both paths work during the shim window.

**Exit criteria:** Config subpackage exists; auth migrated; existing
imports keep working; the empty `config.py` stub is gone.

---

### Phase 3 — Drop the auth shim; add the CI guard

**Goal:** `from alice_core.auth import ...` no longer works. The
dependency-direction CI guard runs in CI.

**Changes:**
- Delete `src/alice_core/auth.py` (the shim).
- Grep audit: any remaining `from alice_core.auth import` callsites
  get fixed to `from alice_core.config.auth import`. Should be zero
  if Phase 2 was thorough.
- Add `tests/test_alice_core_isolation.py`:
  - Walks every `.py` file under `src/alice_core/`.
  - Parses with `ast.parse`.
  - For every `Import` / `ImportFrom`, asserts the top-level module
    is in `{"alice_core", "claude_agent_sdk", *stdlib_modules}`.
  - Fails if any other top-level module name is imported.
- Add the same test as a fixture for any future `alice_core`
  expansion: when adding a new top-level module name, the test
  asserts it's expected.

**Validation:** `pytest tests/test_alice_core_isolation.py`; full
suite green.

**Exit criteria:** Auth shim gone; CI prevents dependency-direction
regression.

---

## Tests

### Existing tests this plan must keep green

- `tests/test_kernel.py` — the kernel construction surface; touched by
  the alice_core internal restructure.
- All other `tests/test_*.py` files — they import from `alice_core`
  via the public surface. Phase 2's `__init__.py` re-exports must
  preserve every public name in use.

### New tests this plan introduces

- `tests/test_alice_core_isolation.py` (Phase 3):
  - `test_alice_core_imports_no_alice_modules` — walks
    `src/alice_core/**/*.py`, asserts no imports of
    `alice_speaking`, `alice_thinking`, `alice_viewer`,
    `alice_watchers`, `alice_prompts`, `alice_skills`,
    `alice_indexer`.
  - `test_alice_core_imports_only_sdk_stdlib_or_self` — assert the
    only top-level modules imported are `claude_agent_sdk`,
    Python stdlib, or `alice_core` itself.

- `tests/test_alice_indexer.py` (Phase 1):
  - `test_indexer_builds_against_fixture_vault` — given a fixture
    `cortex-memory/`, the indexer produces an SQLite DB with the
    expected tables.
  - `test_indexer_check_returns_zero_on_fresh_db`
  - `test_yaml_lite_parses_frontmatter`

(The `cortex_index/` package was previously untested; Phase 1 is the
right time to add a smoke test for the indexer.)

## Risks & non-goals

### Risks

- **Plans 05 and 06 not yet merged when Phase 2 runs.** Mitigation:
  Phase 2 lands the directory structure with auth migrated; plans
  05/06 land their new files directly into `config/` with one-line
  path edits. **This is the cheapest sequencing.** If plans 05/06
  ship first and put files at `alice_core/personae.py` /
  `alice_core/model_config.py`, Phase 2 has to migrate those too —
  doable but more files moving in one phase.

- **External imports of `alice_core.cortex_index`.** The future cue
  runner (planned, per
  `cortex-memory/research/2026-04-28-haiku-cue-runner-auth-investigation.md`)
  is the main expected consumer. If anything in `data/alice-tools/`
  or `data/alice-mind/.claude/` already imports
  `alice_core.cortex_index`, Phase 1's no-shim policy breaks it.
  Mitigation: pre-Phase-1 grep across known consumer locations.

- **The `alice_core/config/` layer adds an import-path level some
  reviewers find ceremonial.** Backwards-compat shims absorb it for
  one phase. After Phase 3, callers update. Effort is mechanical;
  the structural clarity wins.

### Non-goals

- **`KernelSpec` growth management.** After plans 03/05, `KernelSpec`
  gains mode/stage info, system_prompt, per-stage model selection.
  It can plausibly grow to 15+ fields. Resolve in those plans, not
  here.
- **Splitting `kernel.py`.** 357 lines is large but coherent. Defer.
- **Generalizing the dependency-direction guard to all packages.**
  Plan 08 ships only the `alice_core`-only version. A general
  "alice_speaking does not import alice_thinking" guard is a
  follow-up if it proves useful.
- **Moving `session.py` into a `state/` subpackage.** Premature
  abstraction.

## Open questions

1. **Should `config/` be `alice_core/config/` or a top-level
   `alice_config/`?**
   - Top-level: cleaner separation; emphasis that "config is
     independent."
   - Nested: avoids cyclic-dep risk (`config` needs `EventEmitter`
     from `alice_core` for emitting reload events).
   - **Recommendation:** nested under `alice_core/`. Promote to
     top-level only if `config/` exceeds ~5 modules.

2. **Where does `cortex_index/` go?**
   - `alice_indexer/` — terse, parallel to alice_core. **Recommended.**
   - `alice_vault/` — broader; could host other vault tools later.
   - `alice_cortex/` — branded after the vault concept (cortex-memory).
   - **Recommendation:** `alice_indexer` for now; rename if other
     vault tools accrete.

3. **Should the dependency-direction CI guard cover only alice_core,
   or every package?**
   - Plan 00 already names two CI guards (Protocol Conformance,
     Config Liveness).
   - A general dependency-direction guard (`alice_speaking` does not
     import `alice_thinking`, etc.) is a natural extension.
   - **Recommendation:** ship Plan 08's alice_core-only version
     first; generalize in a follow-up if useful.

4. **`session.py` — kernel-level persistence — into a `state/`
   subpackage?**
   - Today it's the only kernel-state file. Premature abstraction.
   - **Recommendation:** leave at top level for now.

5. **What's the right name: `model_config.py` or `model.py`?**
   Plan 06 currently writes `model_config.py`. Inside `config/`,
   `model.py` is symmetric with `auth.py` and `personae.py` — the
   `_config` suffix is redundant once the parent dir says "config."
   **Recommendation:** rename to `config/model.py` during Phase 2
   (or as part of plan 06's merge into the new structure).

## Cross-plan observation

Plan 00 §"Root cause" frames every seam in this refactor as a
variant of "aspirational interface declared, never enforced, became
misleading documentation." The package docstring of `alice_core` is
itself an example: "pure library, no daemon, no entry point" — but
`cortex_index/build_index.py` is a runnable CLI. **Plan 08 brings
the directory in line with its own docstring**, and adds a CI guard
so the next aspirational claim about alice_core gets enforced
on the day it's added.

This makes plan 08 the lowest-stakes, highest-clarity plan in the
corpus. It pays compound interest as the alice_core surface
stabilizes around plans 03–07.
