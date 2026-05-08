# Stage B → ADK workflow — sketch

> **status:** sketch (not a final design). Speaking-side draft 2026-05-08, after PR #18 landed the first stable-script-in-code pattern (alice_metrics.vault_health). Next architectural step: lift Stage B (Consolidation) out of the prompt-driven LLM-orchestrator pattern into an ADK Workflow with typed steps, deterministic flow, and LLM subroutines for the parts that genuinely need judgement.

## Why Stage B and not Stage D / active

Stage B and C are **checklist-driven**. The "intelligence" sits in the side-effects (which note gets a frontmatter fix, which orphan gets linked) — not in the orchestration. The current prompt-fragment pattern asks the model to be both the orchestrator AND the executor on every wake; that's where drift lives (LLM-generated bash, off-by-one filename parsing, ad-hoc subdirectory walks).

Stage D and active mode are the opposite — exploratory, generative. The model's free reasoning IS the work. Those should stay prompt-driven.

So the cut: B and C → ADK workflows. D and active → prompt-driven turns. This sketch covers Stage B as the proof-of-concept; Stage C is parallel work in phase 2.

## Workflow shape

```
StageBWorkflow:
  Step 1: read_wake_state           (deterministic; reads wake.md + state files)
  Step 2: drain_inbox               (loop over notes; each note is one LLM subroutine)
  Step 3: pick_grooming_target      (deterministic; staleness/access scoring)
  Step 4: groom_target              (LLM subroutine: produce a Diff; deterministic apply)
  Step 5: side_checks (parallel)    (each is an optional small LLM subroutine, tight budget)
            ├── stale_finding_lint
            ├── shadow_neighbor
            └── conflict_scan
  Step 6: emit_surfaces             (deterministic; if any step produced surface payloads)
  Step 7: close                     (deterministic; write wake summary, run prune)
```

Each step is a typed function. State threads through as a `WakeState` object with explicit fields. Errors at any step append to `WakeState.errors` rather than throwing — the wake always closes cleanly, even if a step fails.

## Step-by-step

### Step 1 — read_wake_state

```python
@workflow.step
def read_wake_state(ctx: Context) -> WakeState:
    wake_file = ctx.params.wake_file
    return WakeState(
        mode=parse_mode(wake_file),
        time=parse_time(wake_file),
        inbox_files=list_inbox(),
        vault_health=load_latest_vault_health_event(),
        active_thread=load_active_thread(),
    )
```

No LLM. Reads files, returns a typed object. Fails fast if wake_file is malformed.

### Step 2 — drain_inbox

```python
@workflow.step
async def drain_inbox(state: WakeState) -> InboxResult:
    actions = []
    for note_path in state.inbox_files:
        action = await classify_and_route_note(note_path, state.vault_health)
        actions.append(action)
        apply_action(action)  # deterministic file ops
        consume_note(note_path)
    return InboxResult(actions=actions)
```

`classify_and_route_note` is the LLM subroutine. It receives the note body + minimal context (vault state, today's daily existence) and returns a typed `Action` — one of:

```python
@dataclass
class PromoteToVault: target_path: Path; new_content: str
@dataclass
class AppendToDaily: line: str
@dataclass
class CreateConflictNote: ...
@dataclass
class RouteToSurface: surface_payload: dict
@dataclass
class Discard: reason: str
```

The LLM picks one type, fills the fields. `apply_action` does the file write. Deterministic flow, model judgement only at the classification step. Each note is one focused call.

### Step 3 — pick_grooming_target

```python
@workflow.step
def pick_grooming_target(state: WakeState) -> Optional[Path]:
    candidates = score_candidates(
        vault_dir=state.vault_dir,
        criteria=[
            staleness(updated_field, days=14),
            low_access(access_count_field, threshold=2),
            recently_referenced_but_not_groomed,
        ],
    )
    if not candidates:
        return None
    return candidates[0]
```

No LLM. Scoring is deterministic — same inputs always pick the same target, which makes side-effects reproducible across runs. If multiple candidates tie, pick the lexicographically first.

### Step 4 — groom_target

```python
@workflow.step
async def groom_target(state: WakeState, target: Path) -> Optional[Diff]:
    if target is None:
        return None
    current = target.read_text()
    diff = await produce_grooming_diff(
        current=current,
        vault_index=state.vault_index,  # alias resolution, slug map
        constraints=GroomingConstraints(
            preserve_body=True,
            normalize_frontmatter_only=False,  # also fix wikilinks
        ),
    )
    if diff is None:
        return None
    apply_diff(target, diff)
    return diff
```

LLM subroutine — `produce_grooming_diff` — takes the current file + vault index, returns a typed `Diff` (frontmatter changes + wikilink fixes + section-level edits). Apply is deterministic. The model only needs to look at one note plus the index summary; minimal context.

### Step 5 — side checks (parallel branches)

Each side check is its own step with a small budget (≤2 LLM calls). All run in parallel as a fan-out. Any that exceed budget return `None`.

```python
@workflow.parallel
async def side_checks(state: WakeState, target: Path) -> SideCheckResults:
    return await asyncio.gather(
        stale_finding_lint(state, target),
        shadow_neighbor(state, target),
        conflict_scan(state, target),
    )
```

Each one already has a tight scope in the existing prompt — porting them is mostly mechanical. A side check produces either nothing or a small action (link an orphan, mark a note resolved, file a conflict).

### Step 6 — emit_surfaces

```python
@workflow.step
def emit_surfaces(state: WakeState, results: List[StepResult]) -> int:
    payloads = collect_surface_payloads(results)
    for payload in payloads:
        write_surface_file(payload)
    return len(payloads)
```

Deterministic. Each step that wants to surface something fills a `SurfacePayload` in its return object; this step writes them all at the end.

### Step 7 — close

```python
@workflow.step
def close(state: WakeState, results: List[StepResult]) -> WakeSummary:
    summary = build_summary(state, results)
    write_wake_log(summary)
    run_prune()
    return summary
```

Deterministic. Wake log replaces the current `inner/thoughts/<date>/HHMMSS-wake.md` LLM-narrated summary with a structured one (still markdown, but generated from the typed step results, not free text). Prune is the existing rolling-delete pass.

## Telemetry — per-step

Every step emits a telemetry event:

```json
{
  "ts": "...",
  "type": "stage_b_step",
  "step": "drain_inbox",
  "duration_ms": 1400,
  "ok": true,
  "details": {"notes_processed": 3, "actions": ["promote", "append", "discard"]}
}
```

Per-step duration + outcome lets us answer questions the current setup can't: "which step is the long pole?", "is grooming completing or timing out?", "is drain_inbox getting starved by side_checks?". This is the same observability win that PR #18 gave us for vault metrics.

## Failure containment

Per-step errors append to `WakeState.errors` and the workflow continues. This solves yesterday's hung-wake class — if `groom_target` hangs on a model call, the timeout (per-step, e.g., 60s) trips, the step records the error, and the wake closes cleanly. No flock-holding zombie process. Stage 1 of the wake-robustness design (outer timeout) is still the floor; per-step timeouts give earlier resolution.

## Implementation phases

1. **Phase 0 — scaffolding.** New module `alice_thinking/workflows/stage_b/`. Define `WakeState`, the `Action` types, the `Diff` type. No LLM calls yet; just types + tests for `apply_action` and `apply_diff`.

2. **Phase 1 — deterministic steps.** Implement Steps 1, 3, 6, 7 (no LLM). Tests with synthetic vault fixtures asserting correct file-state transitions. End-to-end this gives a "run a Stage B wake that does nothing useful but closes cleanly" path.

3. **Phase 2 — drain_inbox.** Implement `classify_and_route_note` with claude-agent-sdk. Tests use a mocked LLM to assert routing logic for each Action type.

4. **Phase 3 — groom_target.** Implement `produce_grooming_diff`. Tests assert the Diff format and that apply_diff produces the expected file content.

5. **Phase 4 — side checks.** Port the three side checks. Each is a separate step with its own small budget.

6. **Phase 5 — cutover.** Behind a config flag (`thinking.stage_b_workflow_enabled`), route Stage B wakes to the workflow instead of the prompt fragment. Run both in parallel for a few days to compare side-effect outputs (the new workflow should produce a strict subset of the old one's actions, with no novel writes). Then flip the default and remove the prompt fragment.

## What this isn't

- Not a full rewrite of thinking. Active mode and Stage D stay prompt-driven. Stage C gets the same treatment in a follow-up sketch.
- Not a model swap. The LLM subroutines run on the same backend the rest of thinking uses (Qwen via pi-mono today; cloud-routed via per-stage backend if config says so).
- Not portable to Stage D synthesis. Stage D's generative recombination is exactly the kind of work that resists workflow shape.
- Not a skills system. ADK workflows are first-class agentic constructs in code; Anthropic skills are description-matched prompt artifacts. Different tools for different problems. The vault_health work shipped in PR #18 lives at the right level for a skill (single named operation); Stage B's multi-step graph belongs in a workflow.

## Open questions

1. **Workflow runtime** — does the existing pi-mono path support ADK's workflow primitive, or do we need to run the workflow harness on cloud Sonnet and have it call into local Qwen for the subroutines? Likely the latter, which means the Strix Halo plan changes shape: local model for subroutines, cloud orchestrator. Worth a separate investigation before Phase 0.

2. **Diff format** — `Diff` could be JSON-Patch on a parsed AST, or a structured set of (frontmatter_changes, wikilink_changes, section_changes), or a unified-diff. Pick one. Lean toward structured; AST parsing of frontmatter+markdown is overkill for this volume.

3. **Cutover risk** — running both old and new in parallel sounds clean but doubles the LLM cost during the validation window. Alternative: shadow-run the new workflow (no writes, just log what it would do) for a week, compare to actual old-workflow writes. Cheaper, slower to converge.

4. **Sub-budget enforcement** — workflow timeouts are per-step. But the Anthropic ADK documentation (last I read it) doesn't expose a hard SIGKILL on a step that times out the model call. Need to confirm. If not, falls back to outer-process timeout from yesterday's wake-robustness design.

## Next decision Jason owns

Whether to commission Phase 0 + 1 implementation now (just the scaffolding + deterministic steps, ~1 day of worker time, testable end-to-end) or wait until Stage C has a parallel sketch and we commission both together. Recommend the former — Stage B alone is enough to validate the pattern, and waiting blocks the wins for another week minimum.
