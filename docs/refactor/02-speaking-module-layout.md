# 02 — alice_speaking module layout

## Problem

After plan 01 lands, `daemon.py` is small and per-transport handlers live
with their transports — but the top level of `src/alice_speaking/` is still
a heap of 14 unrelated files:

```
src/alice_speaking/
├── __init__.py
├── __main__.py
├── _sanity.py             ← smoke test
├── compaction.py          ← pipeline middleware
├── config.py              ← infra
├── daemon.py              ← still the entry orchestrator
├── dedup.py               ← pipeline middleware
├── events.py              ← infra
├── handlers.py            ← pipeline middleware (BlockHandler impls)
├── principals.py          ← domain
├── quiet_hours.py         ← pipeline middleware
├── render.py              ← domain
├── session_state.py       ← domain
├── signal_client.py       ← infra (Signal JSON-RPC) — confusingly named
│                            alongside transports/signal.py
├── turn_log.py            ← domain
├── tools/                 ← MCP tool definitions (already a subpackage)
└── transports/            ← already a subpackage
```

You cannot tell from a directory listing what kind of code this package
contains. The names don't communicate intent. `signal_client.py` lives
two levels above `transports/signal.py` and they are different things —
the first is a low-level RPC adapter, the second is the inbound transport
implementation. New readers reasonably assume one is the other.

**Three different concerns are mixed at the top level:**

1. **Pipeline middleware** — code that runs around every turn:
   `compaction`, `dedup`, `quiet_hours`, `handlers` (the SDK
   `BlockHandler` implementations).
2. **Domain** — what an Alice turn IS, conceptually: `principals`,
   `render`, `turn_log`, `session_state`.
3. **Infra** — supporting plumbing that doesn't model the domain:
   `config`, `events`, `signal_client` (Signal JSON-RPC adapter, not
   the Signal transport).

There's also one **odd-one-out**: `_sanity.py`, a smoke test. It lives
in the runtime package because it's runnable as `python -m
alice_speaking._sanity`, but it's a test, not a runtime module.

## Goal

After this plan:

- The directory listing of `src/alice_speaking/` communicates the package's
  shape at a glance: a small set of subpackages, each one a recognizable
  concern, each importable from a stable path.
- `signal_client.py` is renamed and relocated so it cannot be confused
  with `transports/signal.py`.
- `_sanity.py` lives somewhere appropriate to a smoke test.
- Existing imports in code outside `alice_speaking` (notably:
  `alice_thinking`, `alice_viewer`, the `bin/` scripts, `tests/`) are
  preserved by **temporary re-export shims** during the refactor and
  cleaned up before the plan closes.
- All tests pass after every phase. No behavior change.

## Design

### Proposed layout

```
src/alice_speaking/
├── __init__.py                  # public re-exports for stable callers
├── __main__.py                  # entry: python -m alice_speaking
├── daemon.py                    # the slim core (post-plan-01)
├── factory.py                   # builds DaemonContext + SourceRegistry (from plan 01)
│
├── pipeline/                    # middleware run around every turn
│   ├── __init__.py
│   ├── compaction.py            # was compaction.py
│   ├── dedup.py                 # was dedup.py
│   ├── handlers.py              # was handlers.py — SDK BlockHandlers
│   └── quiet_hours.py           # was quiet_hours.py
│
├── domain/                      # the model — what a turn is
│   ├── __init__.py
│   ├── principals.py            # was principals.py
│   ├── render.py                # was render.py
│   ├── session_state.py         # was session_state.py
│   └── turn_log.py              # was turn_log.py
│
├── transports/                  # one file per transport (post-plan-01)
│   ├── __init__.py
│   ├── base.py                  # Transport Protocol
│   ├── registry.py              # SourceRegistry (from plan 01)
│   ├── signal.py
│   ├── cli.py
│   ├── discord.py
│   └── a2a.py
│
├── internal/                    # one file per internal source (post-plan-01)
│   ├── __init__.py
│   ├── base.py                  # InternalSource Protocol
│   ├── surfaces.py
│   └── emergency.py
│
├── infra/                       # supporting plumbing
│   ├── __init__.py
│   ├── config.py                # was config.py
│   ├── events.py                # was events.py
│   └── signal_rpc.py            # was signal_client.py — disambiguated name
│
└── tools/                       # unchanged from today (already a subpackage)
    ├── __init__.py
    ├── config_tools.py
    ├── inner.py
    ├── memory.py
    └── messaging.py
```

`_sanity.py` moves out of the runtime package entirely — see Phase 5.

### Naming choices and why

- **`pipeline/`** — these modules run *around* a turn (before, after, or
  observing the SDK stream). "Middleware" was an alternative name; rejected
  because it implies a request/response shape this code doesn't have.
- **`domain/`** — these are the nouns of the model. A `Principal` is a
  thing; a `TurnLog` is a thing; a render is a thing. They have no
  process lifecycle.
- **`infra/`** — config-loading, event-emitter wiring, the raw Signal
  JSON-RPC adapter. Not domain. Not pipeline. Plumbing.
- **`transports/`** — already there. Stays.
- **`internal/`** — the non-transport event sources. Defined in plan 01;
  this plan just respects that decision.
- **`tools/`** — MCP-tool definitions. Already a subpackage. Stays as is.

**Coupling with plan 05.** `tools/` doesn't change *structure* in this
plan, but plan 05 Phase 5 templates the description strings inside
each tool file (rendering through the prompts package against the
loaded personae). That's a content change, not a layout change —
tracked under plan 05. Mention here so reviewers don't assume `tools/`
is fully untouched after both plans land.

### Why rename `signal_client.py` to `infra/signal_rpc.py`

`signal_client.py` is the low-level Signal JSON-RPC adapter — it sends
HTTP-ish requests to the `signal-cli --json-rpc` daemon and parses
responses. `transports/signal.py` is the transport implementation that
*uses* `signal_client.py`. Two files, both with "signal" and "client"
adjacent in their import paths, doing different things. New readers
hit `from .signal_client import ...` and have to dig to figure out
which one is the transport.

`infra/signal_rpc.py` makes the distinction obvious: it's the RPC plumbing,
not the transport. The transport (`transports/signal.py`) imports it as
`from ..infra.signal_rpc import SignalRPC`.

### `__init__.py` discipline

The package's `__init__.py` re-exports the small set of names that external
callers use today. We keep that set stable through the move so callers
(thinking, viewer, bin, tests) don't break. Internal imports use the new
paths.

External callers today (verified by grep, see Phase 0 below):
- `from alice_speaking.config import ...` → public, keep
- `from alice_speaking.principals import ...` → public, keep
- `from alice_speaking.render import ...` → public, keep
- `from alice_speaking.compaction import ...` → public, keep
- `from alice_speaking.daemon import ...` → public, keep

### Alternatives considered

- **Don't reorganize at all; just rename `signal_client.py`.** Cheapest
  fix for the most-confusing naming. But leaves the flat-dir
  problem the user explicitly called out.

- **Group by transport** (`signal/`, `cli/`, `discord/` each with their
  own pipeline + handler files). Rejected — it would duplicate the
  pipeline middleware (compaction, dedup, quiet_hours) per transport,
  or pull them up into a shared dir, which is what this plan does anyway.

- **Promote `tools/` into a top-level package.** The MCP tools ARE
  speaking-side concerns (the agent's outbound capabilities during a
  speaking turn). Keep them under `alice_speaking/`.

- **Use `core/` instead of `infra/`.** Rejected because `alice_core/` is
  already a sibling top-level package. Two `core/` directories invite
  confusion.

## Phases

### Phase 0 — Inventory and shim plan **(blocking gate for Phase 1)**

**Goal:** Before moving anything, enumerate every external import of
`alice_speaking.*` and the test commands that exercise them. Write the
shim list. No code changes yet.

**Changes:** A short follow-up section appended to this plan listing
external import paths to preserve. (Or: a doc-only PR that just lands
this plan.)

**The grep commands to run:**
```
grep -rn "from alice_speaking\." src/alice_thinking/ src/alice_viewer/ \
    src/alice_watchers/ tests/ bin/
grep -rn "import alice_speaking" src/alice_thinking/ src/alice_viewer/ \
    src/alice_watchers/ tests/ bin/
# Also ad-hoc imports from skills/hooks running Python:
grep -rn "alice_speaking" data/alice-mind/.claude/ data/alice-mind/scripts/ \
    data/alice-tools/
```

The output of these greps appears in a "Phase 0 inventory" section
appended to this plan. **Phase 1 does not start until Phase 0's
inventory is captured and reviewed.** This is a hard gate — without it,
a callsite may be silently broken in Phase 7 when shims drop.

**Validation:** Inventory file exists; reviewer confirms no surprises.

**Exit criteria:** External-import inventory captured; the import paths
that must remain stable at the package root are explicitly listed.

---

### Phase 1 — Create `pipeline/` and move middleware

**Goal:** `compaction.py`, `dedup.py`, `quiet_hours.py`, `handlers.py`
move to `alice_speaking/pipeline/`. Old paths re-export.

**Changes:**
- Create `src/alice_speaking/pipeline/__init__.py`.
- `git mv src/alice_speaking/compaction.py src/alice_speaking/pipeline/compaction.py`
  (and the same for dedup, quiet_hours, handlers).
- At the old paths, leave one-line shim modules:
  `from .pipeline.compaction import *  # backwards compat`
  (with a `# Deprecated: import from alice_speaking.pipeline.compaction`
  comment).

**Validation:** `pytest tests/test_compaction.py tests/test_daemon.py
tests/test_signal_batching.py` plus full `pytest`.

**Exit criteria:** Full suite green; new paths exist; shims at old paths
work.

---

### Phase 2 — Create `domain/` and move domain modules

**Goal:** `principals.py`, `render.py`, `turn_log.py`, `session_state.py`
move to `alice_speaking/domain/`. Old paths shim.

**Changes:** Same shape as Phase 1.

**Validation:** `pytest tests/test_principals.py tests/test_session_state.py
tests/test_messaging.py` plus full `pytest`.

**Exit criteria:** Full suite green; old import paths still work via shims.

---

### Phase 3 — Create `infra/`, move + rename plumbing

**Goal:** `config.py`, `events.py` move to `alice_speaking/infra/`.
`signal_client.py` moves to `alice_speaking/infra/signal_rpc.py` (renamed).
Old paths shim.

**Changes:**
- `git mv config.py infra/config.py`
- `git mv events.py infra/events.py`
- `git mv signal_client.py infra/signal_rpc.py`
- Inside `infra/signal_rpc.py`, rename the public class if needed
  (`SignalClient` → `SignalRPC` for symmetry with the new module name).
- Shims at old paths: `signal_client.py` shim is special — it re-exports
  from the renamed location, including under both old and new names so
  any caller using `SignalClient` still works:
  ```
  # alice_speaking/signal_client.py (deprecated)
  from .infra.signal_rpc import SignalRPC as SignalClient
  from .infra.signal_rpc import *
  ```

**Validation:** `pytest tests/test_signal_attachments.py
tests/test_daemon.py` plus full `pytest`.

**Exit criteria:** Full suite green; old paths still work.

---

### Phase 4 — Update internal imports to new paths

**Goal:** Every import inside `alice_speaking/` uses the new paths.
Sibling packages (`alice_thinking`, `alice_viewer`) and tests keep using
the public root-level paths via shims.

**Changes:**
- Walk every `.py` file under `src/alice_speaking/`. Replace
  `from alice_speaking.compaction import` →
  `from alice_speaking.pipeline.compaction import` (and similar).
- Do **not** rewrite imports in tests or in sibling packages — they keep
  using the public root-level imports for now.

**Validation:** `pytest` (full suite); also `python -c "import
alice_speaking; import alice_speaking.daemon; import
alice_speaking.pipeline.compaction; import alice_speaking.domain.principals;
import alice_speaking.infra.config"`.

**Exit criteria:** Internal imports modernized; full suite green.

---

### Phase 5 — Move `_sanity.py` out of the runtime package

**Goal:** `_sanity.py` is a smoke test, not a runtime module. Move to
`tests/smoke/test_sdk_oauth.py` (or `bin/alice-sanity` as a script — see
open questions).

**Changes:**
- `git mv src/alice_speaking/_sanity.py tests/smoke/test_sdk_oauth.py`.
- Adapt to pytest: rename `main()` to `test_sdk_oauth_reaches_claude()`,
  use `pytest.skip` if `CLAUDE_CODE_OAUTH_TOKEN` isn't set in the env.
- Mark `@pytest.mark.smoke` so default `pytest` runs skip it; only
  `pytest -m smoke` runs it (paid test, hits real Claude).
- Update any docs that reference `python -m alice_speaking._sanity`.

**Validation:** `pytest -m smoke` (with token set) returns one passing
test. `pytest` (default) excludes it.

**Exit criteria:** No more underscore-prefixed module in the runtime
package.

---

### Phase 6 — Update the public root API; flag deprecations

**Goal:** Promote the new paths to "preferred" in `__init__.py`.
Deprecation comments stay in the shims.

**Changes:**
- `alice_speaking/__init__.py` re-exports the small public set from
  the new paths:
  ```
  from .infra.config import Config, load
  from .domain.principals import PrincipalBook, PrincipalRecord
  from .domain.render import render_for_transport
  from .pipeline.compaction import COMPACTION_PROMPT, run_compaction
  from .daemon import SpeakingDaemon
  ```
- Each shim file gains a `DeprecationWarning` on import.

**Validation:** `pytest` (full suite); also `python -W error::DeprecationWarning
-c "from alice_speaking.compaction import COMPACTION_PROMPT"` should
emit a deprecation warning.

**Exit criteria:** Suite green; deprecations visible to anyone running
with `-W error::DeprecationWarning`.

---

### Phase 7 — Migrate sibling-package + test imports; drop shims

**Goal:** Update `alice_thinking`, `alice_viewer`, `bin/`, and `tests/` to
use the new paths. Remove the shims.

**Changes:**
- `grep -rn "from alice_speaking\." src/alice_thinking/ src/alice_viewer/
  src/alice_watchers/ tests/ bin/` to find every callsite.
- Rewrite each to the new path.
- Delete the shim files (`alice_speaking/compaction.py`, `dedup.py`,
  `quiet_hours.py`, `handlers.py`, `principals.py`, `render.py`,
  `turn_log.py`, `session_state.py`, `config.py`, `events.py`,
  `signal_client.py`).
- Update `pyproject.toml` `[tool.hatch.build.targets.wheel].packages`
  if the layout change affects what hatch picks up. (It shouldn't —
  `src/alice_speaking` covers all subpackages — but verify.)

**Validation:** `pytest` (full suite); `bin/alice -p "ping"` against a
deployed worker.

**Exit criteria:** Zero shim files; zero references to old paths;
full suite green.

---

## Tests

This plan adds **no new tests** in the conventional sense — it's
mechanical reorganization. What it adds is **import-path tests**:

- `tests/test_imports.py` (Phase 6 or 7):
  - `test_public_root_paths` — `from alice_speaking import Config,
    PrincipalBook, ...` (the public set in `__init__.py`) all work.
  - `test_new_subpackage_paths` — `from alice_speaking.pipeline.compaction
    import COMPACTION_PROMPT` and similar for each new path.
  - `test_old_paths_removed` (Phase 7 only) — `from
    alice_speaking.compaction import COMPACTION_PROMPT` raises
    `ImportError`.

Existing tests this plan must keep green (per phase, listed above):
the entire suite. Nothing should fail at any point.

## Risks & non-goals

### Risks

- **The `tools/` subpackage already exists and is fine.** We are not
  moving it. Don't get clever and put it under `domain/` or `pipeline/`
  — it's its own thing (MCP tool wiring) and the SDK's MCP server
  helper consumes it as a unit.

- **`session_state.py` is small (570 bytes).** It's tempting to inline
  it somewhere. Don't — it's a domain noun and it'll grow.

- **`handlers.py` is `BlockHandler` implementations** (per
  `alice_core.kernel`'s observer protocol), which is *pipeline*
  conceptually but lives in `alice_speaking` because the handlers
  are speaking-side (session persistence, compaction-armer, missed-
  reply detector). This is correct; don't try to push them into
  `alice_core/`.

- **Deprecation warnings noise.** Phase 6 adds `DeprecationWarning`
  emission on shim imports. This will spam during Phase 6/7 before
  callers migrate. The shims live for at most this one plan, so it's
  bounded — but consider running with warnings-as-errors only after
  Phase 7.

- **Hatch packaging.** `pyproject.toml` lists `src/alice_speaking` as
  the wheel target. Subpackages get included automatically by
  hatchling, but the build should be smoke-tested
  (`uv build` or equivalent) at Phase 4 and again at Phase 7.

### Non-goals

- **Not changing public API surface** — `__init__.py` re-exports the
  same names. Code outside this package keeps working without changes
  during phases 1-6.
- **Not splitting `tools/`** — already its own subpackage.
- **Not moving anything between `alice_speaking` and other top-level
  packages** (`alice_core`, `alice_thinking`, `alice_viewer`,
  `alice_watchers`).
- **Not introducing `__all__` or other re-export ceremony** beyond what's
  already there.

## Open questions

1. **Where does `_sanity.py` go in Phase 5?**
   Two reasonable answers:
   - `tests/smoke/test_sdk_oauth.py` — runs under `pytest -m smoke`, no
     new bin entry. **Preferred.**
   - `bin/alice-sanity` — a standalone script. Keeps human-driven smoke
     ergonomics (`bin/alice-sanity` is a familiar shape) but adds another
     bin entry to maintain.

2. **Should we use `core/` or `infra/`?**
   `core/` clashes with the top-level `alice_core` package. `infra/` is
   slightly clinical but unambiguous. **Recommendation: `infra/`.**

3. **Should we promote `pyproject.toml`'s `[tool.hatch.build.targets.wheel]`
   list to a glob (`src/alice_speaking/**`) for clarity?**
   Hatch finds subpackages automatically given the parent — explicit
   sub-listing is unnecessary. **Recommendation: leave as-is**, parents
   already cover children.

4. **Are there any unrecorded import paths used by `data/alice-mind/.claude/`
   skills or hooks?**
   Skills are markdown — they don't import Python. But agents (subagents
   spawned during turns) may run `import alice_speaking.X` ad hoc.
   Phase 0's inventory should include `grep -rn "from alice_speaking\." data/`
   to catch any.

5. **Plan order: should this run interleaved with plan 01, or strictly
   after?**
   Strictly after — plan 01 already does enough moving (event dataclasses
   to transport modules, handler bodies to dispatch module). Running
   them concurrently risks merge churn. **Plan 01 first; this plan
   second.**

6. **Plan 04 collision risk on `compaction.py` and `_sanity.py`.**
   This plan Phase 1 does `git mv compaction.py pipeline/compaction.py`.
   Plan 04 Phase 2 rewrites `compaction.COMPACTION_PROMPT` →
   `templates/speaking/compact.md.j2` — same file, content
   transformation. Plan 02 Phase 5 moves `_sanity.py` to
   `tests/smoke/`; Plan 04 Phase 2 deletes its inline prompt. Mid-
   flight overlap on either file = guaranteed merge churn.

   **Sequencing rule:** Plan 04 Phase 2 must run **either fully
   before Plan 02 Phase 1, or after Plan 02 Phase 7 closes.**
   Recommendation: Plan 02 closes first; Plan 04 Phase 2 then
   modifies content at the new path. The reverse (rewrite first,
   then move) also works but loses the shim-based callsite gradient.

   Mark this in the cross-plan sequencing in plan 00 §"Recommended
   sequence."
