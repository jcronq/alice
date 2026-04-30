# 03 — alice_thinking module layout

## Problem

`src/alice_thinking/` is one 268-line file plus an `__init__.py` and
a `__main__.py`:

```
src/alice_thinking/
├── __init__.py    # 452 bytes
├── __main__.py    # 184 bytes
└── wake.py        # 268 lines
```

`wake.py` does, in one file:

- Argument parsing (`argparse` setup, ~30 lines).
- Auth env loading (delegated, but called from this file).
- Config-overrides resolution (`_apply_config_overrides`, ~25 lines).
- Wake-time-header construction (`_wake_timestamp_header`).
- Prompt assembly (`_build_prompt` — concatenates timestamp + directive
  + bootstrap; reads files from disk).
- Kernel construction + spec building.
- Async run wrapper (`_run_wake`).
- Process exit-code mapping.
- The `main()` entry point.

That's "one wake done end-to-end as a procedure." It's **fine for the
single-mode thinking we have today.** But the agent's own design
documents (`data/alice-mind/CLAUDE.md`, the embedded design notes
above the file viewing) describe a multi-mode architecture that does
not exist in code:

- **Active mode (07:00–23:00)** — draws from `inner/ideas.md` priority
  queue, runs experiments, surfaces actionable findings to the user.
- **Sleep mode (23:00–07:00)** — three sub-stages by time + vault
  state:
  - **Stage B (Consolidation)** — inbox drain, link audit, frontmatter
    normalize, orphan linking.
  - **Stage C (Downscaling, NREM-3 / SWS analog)** — atomize large
    notes, archive stale dailies, merge duplicates, remove orphan stubs.
  - **Stage D (Recombination, REM analog)** — pick 2 recent research
    notes from different domains, look for unexpected connections,
    write a synthesis note.

Today, **none of that exists as code**. It exists as instructions in
the bootstrap prompt (`data/alice-mind/prompts/thinking-bootstrap.md`)
that the agent is asked to interpret each wake. That puts the entire
mode-selection-and-stage-dispatch logic in prose — reasoning the agent
re-derives each wake.

That has three problems:

1. **Untestable.** Mode/stage selection is the agent's reasoning. No
   unit test can assert "at 03:00 with vault stable and a recent
   research corpus, Stage D fires." We have to read wake logs and
   eyeball.
2. **No room to grow.** Adding a new sub-stage (e.g. an "experimentation"
   mode active during certain windows) means editing the bootstrap
   prompt and hoping the agent picks up the change. Not refactor-friendly.
3. **No place for stage-specific code.** Stage D wants different tool
   allowlists, different timeouts, different prompt scaffolding than
   Stage B. Today it's all the same kernel call with a sprawling prompt.

## Goal

After this plan:

- `alice_thinking/` has a directory shape that mirrors its design.
  Adding a new mode or sub-stage is a new file in `modes/` or
  `modes/sleep/`, not a prose edit.
- Mode selection is **code, not prose** — readable, unit-testable, with
  a clear mapping from `(local_hour, vault_state)` → mode/stage.
- Stage-specific config (model, tools, max_seconds, prompt template)
  lives with the stage, not in a single global default.
- The single-mode behavior we have today still works — Phase 1 ships a
  refactor that produces the same wake behavior, just structurally
  cleaner.
- Per-stage telemetry: each wake's emitted events identify which stage
  ran, which makes the viewer (and humans reading wake logs) able to see
  patterns over time.

## Design

### Proposed layout

```
src/alice_thinking/
├── __init__.py
├── __main__.py                # arg parsing + entry; delegates to wake.run()
├── wake.py                    # orchestrator: load config, pick mode, dispatch
├── selector.py                # local hour + vault-state → Mode (pure function, testable)
├── kernel_adapter.py          # KernelSpec construction shared across modes
├── modes/
│   ├── __init__.py
│   ├── base.py                # Mode protocol
│   ├── active.py              # 07:00–23:00 mode: draws from ideas queue
│   └── sleep/
│       ├── __init__.py        # SleepMode dispatcher (sub-stage selection)
│       ├── consolidate.py     # Stage B
│       ├── downscale.py       # Stage C
│       └── recombine.py       # Stage D
├── vault_state.py             # snapshot of vault state used by selector
└── telemetry.py               # mode/stage event emitter helpers
```

### The `Mode` protocol

```
class Mode(Protocol):
    name: str                                # "active" | "sleep:consolidate" | ...

    def kernel_spec(self, ctx: WakeContext) -> KernelSpec:
        """Build the KernelSpec for this mode (model, tools, max_seconds)."""

    async def build_prompt(self, ctx: WakeContext) -> str:
        """Assemble the wake prompt for this mode (template + state injection)."""

    async def post_run(self, ctx: WakeContext, result: KernelResult) -> None:
        """Mode-specific cleanup after the kernel finishes (telemetry, etc.).
        Default: no-op."""
```

`WakeContext` carries the per-wake fixed state (mind path, log path,
local time, vault state snapshot, personae) — same role as
`DaemonContext` in plan 01 but for thinking.

### The selector

```
def select_mode(now: datetime, vault: VaultState, cfg: ThinkingConfig) -> Mode:
    """Pure function: local time + vault state + config → mode.
    No I/O, fully unit-testable."""
    hour = now.hour
    if 7 <= hour < 23:
        return ActiveMode()
    return SleepMode(...)
```

`SleepMode` internally selects its sub-stage based on the full
operational algorithm — **not the simplified time-only version. The
authoritative spec lives in `data/alice-mind/inner/directive.md` Step 0.
Implementation must transcribe that algorithm verbatim, including
three adaptive escape hatches** that today's bootstrap implements:

```
class SleepMode(Mode):
    def _select_stage(self, ctx: WakeContext) -> Stage:
        # Inbox / link issues — always Stage B.
        if ctx.vault.has_pending_inbox or ctx.vault.has_link_issues:
            return ConsolidationStage()

        # Stage D nightly cap exhausted — redirect to Stage B
        # (shadow-neighbor access). Without this, Stage D fires past
        # the nightly 3-synthesis cap.
        if ctx.vault.stage_d_cap_exhausted:
            return ConsolidationStage()

        hour = ctx.now.hour
        if 23 <= hour or hour < 3:
            # Early night, vault stable: Stage C (Downscaling).
            # BUT: if we've null-passed Stage C 6+ times in a row,
            # escape to Stage D (recombination) early — without this
            # we idle on a fully-compressed vault.
            if ctx.vault.consecutive_null_c_wakes >= 6 and ctx.vault.has_recent_research_corpus:
                return RecombinationStage()
            if ctx.vault.is_stable:
                return DownscalingStage()
            return ConsolidationStage()

        # 03:00–07:00 (REM territory).
        # Stage B loops on a stable vault must escape to D after 6+
        # consecutive — otherwise the synthesis window is missed.
        if ctx.vault.consecutive_b_wakes >= 6 and ctx.vault.has_recent_research_corpus:
            return RecombinationStage()
        if ctx.vault.has_recent_research_corpus:
            return RecombinationStage()
        return DownscalingStage()
```

This logic — currently embedded in the bootstrap prompt and re-derived
each wake by the agent — becomes ~30 lines of testable Python. The
three counter fields (`consecutive_b_wakes`, `consecutive_null_c_wakes`,
`stage_d_cap_exhausted`) come from `VaultState` (see below).

**Critical:** the simplified version of `_select_stage` that omits the
counters is a smaller bug exactly of the kind this whole refactor is
designed to eliminate (per plan 00 §"Root cause"). **Phase 4 PR must
diff `_select_stage` against `directive.md` Step 0** before approval —
not against this design snippet, which is illustrative.

### `vault_state.py`

A small module that snapshots vault state at wake start. The selector
reads these to pick a mode/stage. Today the agent reads these files
herself during the wake; moving the snapshot out of the prompt saves
tool calls and centralizes the heuristic.

```python
@dataclass(frozen=True)
class VaultState:
    # Inbox / structural state
    has_pending_inbox: bool
    has_link_issues: bool
    is_stable: bool                     # vault has no notable pending work
    has_recent_research_corpus: bool    # ≥2 research notes in last 24h

    # Adaptive counters — read from inner/thoughts/<today>/*.md
    # frontmatter (`stage:` + `did_work:`) and stage-d-pairs-YYYY-MM-DD.jsonl.
    # Required by SleepMode._select_stage's escape hatches.
    consecutive_b_wakes: int            # consecutive Stage B wakes with did_work=false in last 3h
    consecutive_null_c_wakes: int       # consecutive Stage C wakes with did_work=false in last 3h
    stage_d_cap_exhausted: bool         # tonight's stage-d-pairs.jsonl entries >= nightly cap (3)

    # Misc
    last_groomed_ts: datetime | None
    orphan_count: int
    research_corpus_age_days: float | None
```

The bash recipes for computing the counter fields are already in
`inner/directive.md` Step 0 (the directive's stage-selection
algorithm). Transcribe them into `vault_state.py:snapshot()`.

**Snapshot timing.** The snapshot happens at Python wake-start, before
the agent writes the Step 1 wake file. Counter reads scan
`inner/thoughts/<today>/*.md` for completed wake files only —
the new wake file hasn't been written yet, so it correctly isn't
counted. Test this explicitly.

### Why not just longer prompts?

Today's approach (mode/stage logic in the bootstrap prompt) is one
reasonable choice. The cost:

- Mode selection is non-deterministic — the agent's reasoning under
  prompt drift can pick the wrong stage.
- Each wake spends tokens re-reasoning what could be a 30-line if/else.
- Hard to log "Stage D fired" without trusting the agent to write it.

The proposal: keep the **substantive procedure** in prompts (Stage D's
"pick 2 research notes, look for unexpected connections" stays in a
prompt — that's actual reasoning work). Move only the **dispatch** (which
stage runs given current state) to code.

### Alternatives considered

- **Leave it as one file.** Defensible — `wake.py` is only 268 lines.
  But the README and CLAUDE.md describe a much larger surface that
  doesn't exist anywhere. This plan brings the code shape into
  alignment with the design.

- **Strategy pattern via a registry instead of explicit selector.**
  Slightly cleaner for adding modes, slightly noisier for reading the
  dispatch logic. The 4 modes today fit cleanly into a single
  `select_mode()` function; promote to registry only if we exceed ~6
  modes.

- **Make mode selection an LLM call (Haiku) at the start of each
  wake.** Adds latency; the heuristic is simple enough to be code.
  Reject.

## Phases

### Phase 1 — Extract pieces of `wake.py` into siblings

**Goal:** Same single-mode behavior; just split the file. No
modes/sleep/active concept yet.

**Changes:**
- `selector.py` — empty (placeholder, returns a constant `ActiveMode`-like
  object that wraps the existing single-mode behavior).
- `kernel_adapter.py` — extracts the `KernelSpec` construction code
  currently inlined in `_run_wake`.
- `wake.py` — slimmed to: load token, load config, build context,
  select mode (always returns the placeholder), call mode's prompt
  builder + kernel call, exit.
- The argparse + entry stays in `__main__.py` (and a `main()` shim in
  `wake.py` for backwards compat with `bin/alice-think`'s
  `python -m alice_thinking.wake` invocation, if any).

**Validation:** Add `tests/test_thinking_wake.py`:
- `test_wake_builds_prompt_with_directive_and_bootstrap` — golden
  test: given fixture mind dir with bootstrap.md + directive.md,
  the assembled prompt has the expected structure.
- `test_wake_falls_back_when_no_directive` — bootstrap.md exists
  but directive.md doesn't; prompt still assembles.
- `test_wake_quick_mode_uses_quick_prompt` — `--quick` flag bypasses
  bootstrap.

Plus: `bin/alice-think --quick` still exits 0 against a deployed
worker (manual; existing smoke).

**Exit criteria:** `pytest tests/test_thinking_wake.py` green; full
suite green; thinking still wakes.

---

### Phase 2 — Define `Mode` protocol; introduce `ActiveMode` as the only mode

**Goal:** Codify the protocol. The single mode we have today becomes
`ActiveMode`. Selector is a one-liner that returns it.

**Changes:**
- `modes/base.py` — `Mode` Protocol.
- `modes/active.py` — `ActiveMode` class. Its `build_prompt` reads the
  bootstrap + directive (current behavior). Its `kernel_spec` returns
  the current kernel spec. `post_run` is a no-op.
- `selector.py` — `select_mode(now, vault, cfg)` returns `ActiveMode()`
  unconditionally.
- `wake.py` — calls `selector.select_mode()`, then `mode.build_prompt`,
  then `kernel.run()`, then `mode.post_run`.

**Validation:** Add `tests/test_thinking_modes.py`:
- `test_active_mode_implements_protocol`
- `test_selector_returns_active_mode_for_current_logic`
  (placeholder — selector is a one-liner)
- All Phase-1 tests still green.

**Exit criteria:** Same wake behavior; protocol exists; one mode is
implemented.

---

### Phase 3 — Add `vault_state.py`; selector dispatches by hour

**Goal:** Wake-time hour determines mode (active vs sleep). SleepMode
exists as a stub that delegates to ConsolidationStage (since that's
the most-defensible default per today's behavior).

**Changes:**
- `vault_state.py` — `VaultState` dataclass + `snapshot(mind_path) ->
  VaultState`. Reads inbox count, last-groomed timestamp,
  research-corpus presence, etc. Cheap I/O at wake start.
- `modes/sleep/__init__.py` — `SleepMode` class with sub-stage
  dispatch (currently always returns `ConsolidationStage`).
- `modes/sleep/consolidate.py` — Stage B implementation. Initially
  identical to ActiveMode (delegates the same prompt). This is a
  code-shape phase, not a behavior phase.
- `selector.py` — `if 7 <= hour < 23: ActiveMode() else: SleepMode()`.
- `wake.py` — passes `VaultState` into the context.

**Validation:**
- `test_selector_returns_sleep_mode_for_2300_local`
- `test_selector_returns_active_mode_for_1000_local`
- `test_sleep_mode_returns_consolidation_stage_when_inbox_pending`
- Telemetry: every wake's events now include a `mode` field. Assert in
  `tests/test_thinking_wake.py::test_wake_emits_mode_event`.

**Exit criteria:** Modes dispatched by hour; telemetry shows mode;
behavior unchanged because all modes share the same prompt.

---

### Phase 4 — Differentiate Stage B / C / D **(behavior change)**

**Goal:** Stages diverge in prompt, tool allowlist, and (optionally)
model. Each stage has its own file.

**Changes:**
- `modes/sleep/downscale.py` — Stage C: prompt template focuses on
  atomize / archive / merge ops. Allowlist subset.
- `modes/sleep/recombine.py` — Stage D: prompt template focuses on
  cross-domain synthesis. Tighter `max_seconds` (3-4 tool calls).
- `modes/sleep/__init__.py` — full stage selector logic from the
  Design section (`_select_stage`).
- Active mode keeps its prompt as-is.

**This is a behavior change.** The agent stops re-deriving stage
selection in prose; she now receives a stage-specific prompt up front.
Mark this phase explicitly. Validate by reading wake logs for a few
days post-deploy.

**Validation:**
- `test_sleep_mode_picks_downscaling_after_2300_when_vault_stable`
- `test_sleep_mode_picks_recombination_after_0300_with_recent_research`
- `test_sleep_mode_picks_consolidation_when_inbox_pending`
- Manual: read 1 day of wake logs, confirm stages fire as expected.

**Exit criteria:** Three sub-stages distinct; selector logic unit-tested;
the bootstrap prompt no longer contains stage-dispatch logic (it's been
replaced by per-stage prompts).

---

### Phase 5 — Move stage prompts to the prompts package (depends on plan 04)

**Goal:** Each stage's prompt is a file in the prompts package, loaded
by name. Stops embedding multi-paragraph templates as Python f-strings.

**Bootstrap is two distinct things; keep them separate.** Today's
`thinking-bootstrap.md` mashes together (a) immutable structural
instructions for the wake cycle (move to per-stage `.md.j2` templates
in the prompts package) and (b) `inner/directive.md`, which Jason
edits as standing operational orders (stays in the mind, gets
runtime-injected into each stage's template via `{% include directive %}`).
Different lifecycles → must remain different files. See plan 00
§"Thinking-bootstrap migration handoff".

**Changes (only after plan 04 has shipped Phase 1):**
- Move `wake.QUICK_PROMPT`, `_wake_timestamp_header` template, each
  mode's prompt template into the prompts package as
  `thinking.wake.<mode>.md.j2` (or `thinking.wake.sleep.<stage>.md.j2`
  for the three sleep stages).
- The directive stays at `mind/inner/directive.md` and is loaded as a
  template variable, not as part of the template itself.
- `mode.build_prompt` calls `prompts.load("thinking.wake.<mode>",
  directive=read_directive(), ...)`.

**Validation:** `pytest tests/test_thinking_*` green; existing wake
behavior preserved.

**Exit criteria:** No multi-paragraph prompt strings in the thinking
package; all loaded from the prompts package.

---

## Tests

### Existing tests this plan must keep green

- `tests/test_kernel.py` — kernel observer pattern; thinking uses the
  kernel directly, so behavior must not change.

(There is no `tests/test_thinking_*.py` today. Phase 1 introduces the
first one.)

### New tests this plan introduces

- `tests/test_thinking_wake.py` (Phase 1):
  - `test_wake_builds_prompt_with_directive_and_bootstrap`
  - `test_wake_falls_back_when_no_directive`
  - `test_wake_quick_mode_uses_quick_prompt`
  - `test_wake_emits_mode_event` (Phase 3)
  - `test_wake_passes_vault_state_to_mode` (Phase 3)

- `tests/test_thinking_modes.py` (Phase 2+):
  - `test_active_mode_implements_protocol`
  - `test_active_mode_kernel_spec_uses_default_model`
  - `test_sleep_mode_implements_protocol`
  - `test_sleep_mode_picks_consolidation_when_inbox_pending`
  - `test_sleep_mode_picks_downscaling_after_2300_when_vault_stable`
  - `test_sleep_mode_picks_recombination_after_0300_with_recent_research`

- `tests/test_thinking_selector.py` (Phase 3):
  - `test_selector_returns_active_mode_for_1000_local`
  - `test_selector_returns_sleep_mode_for_2300_local`
  - `test_selector_dst_aware` — Mar/Nov DST transitions don't break
    the selector (uses `zoneinfo`, not hour offsets).
  - `test_selector_uses_injected_clock_for_testability` — pass `now`
    explicitly.

- `tests/test_vault_state.py` (Phase 3):
  - `test_snapshot_counts_inbox_files`
  - `test_snapshot_detects_recent_research_corpus`
  - `test_snapshot_handles_missing_mind_dir_gracefully`
  - `test_snapshot_counts_consecutive_b_wakes_in_last_3h`
  - `test_snapshot_counts_consecutive_null_c_wakes_in_last_3h`
  - `test_snapshot_detects_stage_d_cap_exhausted`
  - `test_snapshot_excludes_in_progress_wake_file`

- `tests/test_thinking_modes.py` extension (Phase 4):
  - `test_sleep_mode_picks_recombination_when_consecutive_b_exceeds_6`
  - `test_sleep_mode_picks_recombination_when_consecutive_null_c_exceeds_6_with_corpus`
  - `test_sleep_mode_redirects_to_consolidation_when_stage_d_cap_exhausted`
  - `test_select_stage_matches_directive_step_0_algorithm` —
    transcription check: every branch of `directive.md` Step 0 maps
    to exactly one branch in `_select_stage`.

## Risks & non-goals

### Risks

- **Phase 4 is a real behavior change.** The agent currently re-derives
  the stage from prose; switching to code-driven dispatch may surface
  bugs in stages that were silently never running (e.g. the agent
  always stayed in Consolidation despite the bootstrap saying otherwise).
  Plan to roll out behind a `thinking.dispatch_mode: "code" | "prompt"`
  config knob, default "prompt" for Phase 4 PR; flip to "code" after
  shadow-running the code path for ~24h and comparing emitted modes
  against agent-emitted-prose.

  **Shadow-run criteria (must define before Phase 4 PR):**
  - How is the code-derived stage surfaced when running in "prompt"
    mode so discrepancies are observable? (Recommendation: emit
    `mode_shadow` event with the code-derived choice every wake;
    viewer renders it next to the agent's actual choice.)
  - What constitutes a "match" vs "divergence"? (Recommendation:
    same Stage = match; different Stage = divergence.)
  - Criterion for flipping default to "code"? (Recommendation: ≥95%
    match rate over 48 consecutive hours, no critical-path
    divergences.)

- **Vault state snapshot adds I/O at wake start.** Today the agent
  reads `inner/notes/` etc. mid-prompt. Pre-snapshotting saves agent
  tool calls but adds Python I/O. Watch for snapshot latency in
  `wake_start` → first-event timing.

- **DST handling.** Selector takes a tz-aware `datetime`; verify the
  zoneinfo path explicitly with a Mar/Nov transition test.

- **Backward compat for `--prompt` and `--bootstrap` CLI flags.** The
  ad-hoc invocations in `bin/alice-think` use these. Phase 1 must not
  break them. The wake's CLI surface stays exactly the same.

### Non-goals

- **No new modes / stages introduced** — this plan codifies what's
  already in the design docs. New modes are post-refactor work.
- **No change to the cron cadence** — s6 still calls `alice-think`
  every N minutes; Alice-mind config controls the interval.
- **No subagent / worker spawn from thinking** — thinking is still
  read-mostly; subagent spawning is speaking's job (per CLAUDE.md
  §Hemisphere boundary).
- **No persistent state about previous wakes** — each wake is fresh.
  The vault is the persistent state.

## Open questions

1. **Should `select_mode` be configurable via `mind/config/alice.config.json`?**
   Today's `thinking.*` config block already has `rem_cadence_minutes`
   and `active_cadence_minutes`. Adding `thinking.modes.*` for explicit
   overrides (e.g. force-active during certain hours, force-sleep
   during others) would be useful for testing and for users with
   non-default schedules. **Recommendation: yes, in Phase 3.**

2. **Should `VaultState` be cached across wakes?**
   No — wakes are 5 minutes apart minimum and vault state can change
   in between (notes can be appended by speaking). Snapshot every wake.

3. **Should each stage carry its own model?**
   Today: all wakes use `claude-sonnet-4-6`. The Recombination stage
   may benefit from Opus for the synthesis step. Adding per-stage
   `model` is cheap (`KernelSpec.model`); add it in Phase 4 when each
   stage gets its own `kernel_spec()`.

4. **Should the bootstrap prompt go away entirely?**
   Long-term: yes. Each mode's `build_prompt` is the equivalent. But
   Phase 5 (depends on plan 04) is when this happens — until then
   keep the bootstrap as a fallback the modes can include.

5. **Where do `inner/directive.md` and `inner/ideas.md` fit in the
   new structure?**
   These are state inputs to the mode, not prompts. `vault_state.py`
   reads them into the snapshot. `ActiveMode.build_prompt` injects the
   directive + the top of the ideas queue. `SleepMode` typically
   ignores `ideas.md` (sleep is not generative; it's grooming).
