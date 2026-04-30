# Refactor — Overview

This directory holds the implementation plans for a structural cleanup of the
Alice runtime. Each numbered file is a self-contained plan: problem statement,
design, phased implementation, tests, risks. Plans are sequenced — earlier
ones unblock later ones — but each plan stands alone and ships independently.

This file is the index, the dependency graph, the cross-cutting principles,
and the explicit non-goals.

## The nine seams

The refactor targets nine specific structural problems, identified by reading
the code (not by speculation):

1. **Transports are pluggable in name only.**
   `daemon.py:113-167` defines six event dataclasses; `daemon.py:560-576`
   dispatches them with an `isinstance` ladder; each new transport requires
   five separate edits across two layers. → [01-transport-plugin-interface.md](01-transport-plugin-interface.md)

2. **`SpeakingDaemon` is a 1914-line god-class.**
   Config reload, queue producer, queue consumer, batching, compaction
   trigger, six per-transport handlers, outbox routing, principal lookup —
   all one class sharing state through `self`. → folded into plan 01.

3. **`alice_speaking/` flat dir hides three concerns.**
   Pipeline middleware (`compaction`, `dedup`, `quiet_hours`, `handlers`),
   domain (`principals`, `turn_log`, `session_state`, `render`), and
   infra (`events`, `config`, `signal_client` — confusingly named alongside
   `transports/signal.py`) all sit at the top level. → [02-speaking-module-layout.md](02-speaking-module-layout.md)

4. **`alice_thinking/` is one 268-line file with no room to grow.**
   `README.md:121-124` promises active vs sleep/REM modes with
   consolidation/downscaling/recombination sub-stages — none of which exists
   in code. The package shape doesn't match the design. → [03-thinking-module-layout.md](03-thinking-module-layout.md)

5. **Prompts are scattered across code and the mind repo.**
   `compaction.py:29` (`COMPACTION_PROMPT`), `_sanity.py:31` (inline
   `system_prompt=`), `wake.py:50` (`QUICK_PROMPT`), `narrative.py:128/353/481`
   (four LLM-summary prompts), per-handler prompt assembly in `daemon.py`,
   `mind/prompts/*.md`, `inner/directive.md`. No index, no naming convention,
   no template engine. → [04-prompts-package.md](04-prompts-package.md)

6. **Personae (agent + user identity) are not actually injected.**
   `templates/mind-scaffold/HEMISPHERES.md:160` claims "system-prompt
   injection of SOUL.md, IDENTITY.md, CLAUDE.md, USER.md once per process
   lifetime." Grep confirms zero `system_prompt=`/`append_system_prompt=`
   calls in the runtime kernel/daemon/handlers/config. The agent's persona
   is held together by Claude Code's cwd-CLAUDE.md auto-load — a host-CLI
   behavior, not Alice runtime code. The name "Alice" is hardcoded in ~30
   sites; "owner" in ~10. → [05-personae-and-injection.md](05-personae-and-injection.md)

7. **Backend selection is implicit and limited.**
   The Claude Agent SDK can route to subscription (Max OAuth), API key
   (incl. LiteLLM proxy), AWS Bedrock, and Google Vertex — but Alice
   only exposes the first two, and selection happens implicitly via
   env-var precedence in `auth.py:99-104`. Bedrock isn't wired. There's
   no per-hemisphere backend selection (speaking and thinking share one
   auth mode). Model IDs and backend choice live in different files,
   making the "swap to Bedrock for thinking, stay on subscription for
   speaking" workflow a multi-file edit. **The SDK stays as the only
   kernel implementation** — what's missing is config-driven backend
   selection. → [06-provider-port.md](06-provider-port.md)

8. **Skills are markdown files in a directory the runtime cannot see.**
   Skills (`mind/.claude/skills/<name>/SKILL.md`) are auto-loaded by the
   Claude Code CLI when it runs with `cwd=alice-mind`. The runtime
   (`alice_speaking`, `alice_thinking`) does not know skills exist — there
   is no skill registry, no telemetry of skill firings, no per-hemisphere
   scoping (speaking and thinking share the same set), no description
   templating with the user's actual name, no test harness, and no
   override / inheritance story. → [07-skills.md](07-skills.md)

9. **`alice_core/` claims to be a pure library and isn't.**
   Its own docstring says "pure library, no daemon, no entry point" —
   but `cortex_index/build_index.py` is a 407-line runnable CLI that
   walks a vault. Three new files arrive from plans 05 + 06
   (`personae.py`, `model_config.py`) without a structural home, and
   nothing prevents `alice_core` from inverting its dependency
   direction (importing from `alice_speaking`, etc.) on the day a
   convenient field assignment makes it tempting. The kernel package
   needs the same intent-communicating layout the refactor brings to
   the hemispheres, plus a CI guard that enforces dependency
   direction. → [08-alice-core-organization.md](08-alice-core-organization.md)

## Dependency graph

```
01 (transports + daemon decomp) ──> 02 (alice_speaking layout)
                                          │
04 (prompts package) ─────────> 05 (personae + injection) ──> 07 (skills)
                                          │            ↑
                                          │            │ depends on plan 08
                                          │            │ structure being in place
                                          │
03 (alice_thinking layout)  ─ independent (small)
08 (alice_core organization) ─ independent; ship before 05 + 06 so
                              their new files land into config/ directly
06 (backend selection)      ─ independent; depends on plan 08 for
                              landing model.py into config/
```

- **01 unblocks 02:** the speaking-layout reorg has cleaner targets once
  per-transport handlers live with their transports.
- **04 unblocks 05:** personae substitution needs a template loader.
- **03 is independent** — it touches a single 268-line file and one s6
  service. Can ship any time.
- **06 is independent** and now narrow (config-driven backend selection,
  no kernel rewrite). Ship any time after Phase 1 of plan 04 (so
  `model.yml` can sit alongside the other config the prompts package
  reads, though they're separate files).

## Recommended sequence

```
01 → 02 → 04 → 08 → (05 || 06) → 03 → 07
```

`08` slots in after `04` and before `05`/`06` so the latter two land
their new config files directly into `alice_core/config/`. `05` and
`06` can ship in parallel after `08`. `03` follows `05` (it consumes
plan 04's templates and plan 05's personae). `07` is last (depends on
04, 05, and indirectly on 08).

**Phase-level coupling between 02 and 04.** Plan 02 Phase 1 moves
`alice_speaking/compaction.py` to `pipeline/compaction.py`; Plan 04
Phase 2 rewrites `compaction.COMPACTION_PROMPT` into a template at
the new location. Same file, different operations. Same hazard
applies to `_sanity.py` (Plan 02 Phase 5 moves it; Plan 04 Phase 2
deletes its inline prompt). **Plan 04 Phase 2 must run either
fully before Plan 02 Phase 1, or after Plan 02 Phase 7 closes.**
Recommendation: 02 closes first, then 04 Phase 2 modifies content at
the new path. The reverse also works but loses the shim-based
callsite gradient. See plan 02 §"Open questions" Q6.

Rationale:

- **01 first** — it unblocks the most other planks and produces the biggest
  immediate code-readability win (daemon shrinks ~60%).
- **02 next** — pure cleanup once 01's daemon decomposition makes the
  module groupings obvious.
- **04 then 05** — prompts are the prerequisite for real persona injection;
  doing them in order avoids backtracking.
- **03 mid-sequence** — small, isolated, and the active/sleep mode work
  starts mattering as Thinking matures.
- **06 last** — biggest blast radius; do it once everything else is stable.

## Root cause

Every seam in this refactor is a variant of the same failure mode,
and saying it explicitly is part of the fix:

> **Aspirational interface declared → implementation never catches up
> → declaration becomes misleading documentation.**

Examples:

- `transports/base.py` exists but adding a transport requires five
  edits (plan 01).
- `SpeakingDaemon` was supposed to be an event loop; grew to 1914
  lines (plan 01).
- `alice_thinking/README.md` promises five sub-stages that don't
  exist in code (plan 03).
- `templates/mind-scaffold/HEMISPHERES.md:160` claims persona
  injection; zero `system_prompt=` calls exist at runtime (plan 05).
- `rate_limit_policy` config key is defined and documented but never
  read by any code path.

**Reorganizing the code without adding enforcement reproduces the
same bug a year from now.** The plans below introduce new aspirational
interfaces (`Transport`, `InternalSource`, `StartupSource`,
`PromptLoader`, `SkillRegistry`, `Personae`); without guardrails,
each becomes a future seam.

### Cross-cutting CI guards

Two cheap CI tests prevent the recurrence pattern. Both are
~30 lines; both should land as part of plan 01's exit criteria, not
deferred to a later plan:

1. **Protocol-conformance test at module boundary.**
   `tests/test_module_boundaries.py`:
   - Every file in `transports/` (other than `base.py`/`registry.py`)
     contains exactly one class implementing the `Transport` protocol.
   - Same for `internal/` and `InternalSource`.
   - Same for `alice_skills/` once plan 07 lands and `Skill`.
   Turns "you forgot to wire the new transport" from a runtime
   surprise into a CI failure.

2. **Config-liveness test.**
   `tests/test_config_liveness.py`:
   - Walk `mind/config/alice.config.json`, `mind/config/model.yml`
     (plan 06), and `mind/personae.yml` (plan 05) for every leaf key.
   - For each key, grep the source tree for at least one read.
   - Fail if any key has zero reads.
   If `rate_limit_policy` had had this test, the dead code would
   have been caught the day it was added.

3. **Loader call-site → template existence.**
   `tests/test_template_call_sites.py`:
   - AST-walk the codebase for every `prompts.load("X.Y", ...)` call
     site (plan 04) and every `skills.find("name")` call site (plan 07).
   - For each named template / skill, assert the corresponding file
     exists in the registry's search paths.
   - Catches typos and "added a new transport but forgot the
     template" failures at CI time, not on the first live event.
   Plan 04 Phase 5 introduces the first call site for which this
   guard matters; institutionalizing it here keeps the next round of
   plans from rediscovering the same shape via manual review.

These guards make it expensive to add a new aspirational interface
without wiring it; all three should live in `tests/` so they run on
every PR. The principle they embody: **whenever the plan introduces
a registry, a config schema, or a name-based lookup, ship a test
that walks that registry / schema / lookup and asserts every
declared interface has a wiring.**

## Cross-cutting principles

These apply to every plan. Plan-specific phases must respect them.

### Every phase leaves the agent runnable and tested

Each phase is shippable on its own. After every phase:

- `pytest` is green (full suite, not just one file).
- `bin/alice -p "ping"` returns a reply when run against a deployed
  worker (manual smoke; automated equivalent is `tests/test_kernel.py`
  + transport tests).
- The two hemispheres still wake. (Manual: `bin/alice-think --quick`;
  automated equivalent is the existing kernel tests.)

If a phase can't satisfy these, it's two phases.

### Backwards-compat shims live for one plan only

When a phase moves a module, it leaves a shim re-exporting from the old
path so unrelated callers keep working. Shims are removed in the **final
phase of the same plan**, not deferred. A shim that survives a plan
boundary becomes a permanent crutch — that's how Alice's current layout
got the way it is.

### Validation is a single command

Each phase declares one command — usually a `pytest` invocation, sometimes
a `pytest` plus a script — that proves the phase works. "Manually verify
the daemon starts" is not a valid validation. If it can't be a command,
write the test first.

### Tests added by these plans are unit-level by default

Existing tests in `tests/` mostly stub out transports and the SDK
(`tests/test_daemon.py`, `tests/test_a2a_transport.py`, etc.). New tests
follow the same pattern: fakes/stubs at the SDK boundary, real code under
test. Integration tests against a real daemon are **out of scope** for the
refactor — keep them in the existing one-off scripts (`bin/alice -p
"ping"`) for human-driven smoke.

### Plans don't change behavior on purpose

Refactor plans must preserve user-visible behavior. The personae plan (05)
is the explicit exception — it changes what's in the system prompt, which
is a behavior change. Mark behavior-changing phases with **(behavior
change)** in their headers so the reviewer can be deliberate.

## Test infrastructure (current state)

Recorded here so each plan can reference real commands.

- **Runner:** `pytest` (configured in `pyproject.toml` — `pythonpath=["src"]`,
  `testpaths=["tests"]`, `asyncio_mode = "strict"`).
- **Existing test files** (in `tests/`):
  - `test_kernel.py` — agent kernel + observer pattern
  - `test_daemon.py` — speaking daemon producer/consumer + handlers
  - `test_compaction.py` — context compaction handler
  - `test_messaging.py` — `send_message` tool
  - `test_principals.py` — address book + ACL
  - `test_a2a_transport.py` — A2A inbound
  - `test_discord_transport.py` — Discord inbound
  - `test_signal_attachments.py` — signal media handling
  - `test_signal_batching.py` — coalesced-burst handling
  - `test_session_state.py` — session resume marker
  - `test_gh_watcher.py` — GitHub poller (the watcher I just added)
- **Pattern:** stub the SDK + transport-IO boundary; exercise our code.

A passing run currently looks like `pytest` exiting 0 with all of the above
green. That's the bar each phase must clear.

## Cross-plan handoffs and gaps

Two coordination items don't fit cleanly inside any single plan:

### Thinking-bootstrap migration handoff

Plan 04 (prompts package) Phase 6 moves `wake.py`'s prompt assembly
into the loader. Plan 03 (thinking layout) Phase 5 moves per-stage
prompts into the loader. Between those two, **someone has to write
the per-stage `.md.j2` files** that contain the substance of today's
400-line `thinking-bootstrap.md`. Neither plan owns it; today's
bootstrap is two distinct things mashed together:

1. **Immutable structural instructions** — wake-cycle skeleton, mode
   selection algorithm. Should become per-stage templates in
   `src/alice_prompts/templates/thinking/`. Owned by plan 04 Phase 6.
2. **`inner/directive.md`** — Jason-edited operational standing
   orders. Lives in the mind, not in the prompts package, even after
   migration. Loaded as a runtime variable; injected into the active
   stage's template via `{% include directive %}`. Owned by plan 04.

These two **must remain separate files** even after Phase 5 / Phase 6
land. The migration handoff: plan 04 ships first, plan 03 Phase 5
consumes its templates. Mark plan 04 Phase 6 as a gate for plan 03
Phase 5.

### `alice_core/` rationalization (now plan 08)

This was originally framed as a "may emerge later" gap. Alice's
review of plans 02–07 (her cortex-memory research notes, 2026-04-29)
correctly identified that **doing it later costs more** — plans 05
and 06 will deposit their new config files into `alice_core/`'s
top level, and we'll then have to migrate them into a `config/`
subpackage retroactively. Cheaper sequencing: do plan 08 first, let
plans 05 and 06 land their files into the right structure on
arrival.

→ See [08-alice-core-organization.md](08-alice-core-organization.md).

### Cue-runner sequencing risk

The Haiku cue runner (designed in
`cortex-memory/research/2026-04-28-haiku-cue-runner-auth-investigation.md`
and related notes) integrates at `daemon.py:_compose_prompt` around
line 1528. Plan 01 Phase 6 deletes that method — prompt composition
moves into transport handlers.

**The cue runner should not ship before plan 04 (prompts package)
is at Phase 5 or later.** After plan 04, the cue runner integrates
cleanly as prompt middleware: a function the prompt loader calls to
prepend a reference packet before rendering. Before plan 04, it
needs to wrap a method that's about to be deleted.

## What's deliberately out of scope

These came up while reading the code; they're real but not in this refactor:

- **Viewer redesign.** The viewer has its own structural issues
  (`aggregators.py` is 1157 lines, `sources.py` is 1004) but the viewer
  is read-only and replaceable. Refactor the runtime first.
- **Mind-repo restructure.** Plans here only touch `templates/mind-scaffold/`
  where it intersects with personae loading. Reorganizing the mind itself
  (cortex-memory layout, daily logs, events.jsonl schema) is the agent's
  job, not the runtime's.
- **Performance.** No phase claims a perf win; none are intended.
- **New transports / new providers.** Plans give the *interface* room to
  add them; adding them is post-refactor work.
- **Sandbox / container layout.** `sandbox/worker/Dockerfile` is fine.
  The runtime refactor doesn't change what gets baked into the image.
- **Docs in the mind.** `IDENTITY.md`, `SOUL.md`, `USER.md`, `HEMISPHERES.md`
  in the mind scaffold are the agent's documents. Plan 05 is careful not to
  delete them — it just makes runtime stop depending on her remembering to
  read them.

## How to read each plan

Every plan file has the same shape:

- **Problem** — concrete observations with file:line citations.
- **Goal** — what "done" looks like, testably.
- **Design** — the proposed architecture: module names, class names,
  key interfaces. No code, but specific enough that a reader could write
  the code from the description.
- **Phases** — numbered, each with goal / changes / validation command /
  exit criteria. Each phase is one PR's worth of work.
- **Tests** — both existing tests that protect against regression and new
  tests this plan introduces, with one sentence per test on what it asserts.
- **Risks & non-goals** — known sharp edges; known things deliberately
  not addressed.
- **Open questions** — calls the plan author wants the reviewer to make
  before implementation starts.

If something's missing from a plan, that's a bug. Open a follow-up.
