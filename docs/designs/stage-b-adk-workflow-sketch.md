# Stage B → google-adk workflow — sketch

> **status:** active. Speaking-side draft 2026-05-08, after PR #18 landed
> the first stable-script-in-code pattern (alice_metrics.vault_health).
> The ADK port (this PR) supersedes the native-Python prototype in
> the now-closed PR #19.

## Why Stage B and not Stage D / active

Stage B and C are **checklist-driven**. The "intelligence" sits in
the side-effects (which note gets a frontmatter fix, which orphan
gets linked) — not in the orchestration. The current prompt-fragment
pattern asks the model to be both the orchestrator AND the executor
on every wake; that's where drift lives (LLM-generated bash,
off-by-one filename parsing, ad-hoc subdirectory walks).

Stage D and active mode are the opposite — exploratory, generative.
The model's free reasoning IS the work. Those should stay
prompt-driven.

So the cut: B and C → ADK workflows. D and active → prompt-driven
turns. This sketch covers Stage B as the proof-of-concept; Stage C
is parallel work in phase 2.

## Workflow shape

```
SequentialAgent (stage_b_workflow):
  Step 1: read_wake_state           (deterministic; reads wake.md + state files)
  Step 2: drain_inbox               (loop over notes; each note is one LLM subroutine)
  Step 3: pick_grooming_target      (deterministic; staleness/access scoring)
  Step 4: groom_target              (LLM subroutine: produce a Diff; deterministic apply)
  Step 5: side_checks (parallel)    (each is an optional small LLM subroutine, tight budget)
            ParallelAgent:
              ├── stale_finding_lint
              ├── shadow_neighbor
              └── conflict_scan
  Step 6: emit_surfaces             (deterministic; if any step produced surface payloads)
  Step 7: close                     (deterministic; write wake summary, run prune)
```

Each step is a tiny ``BaseAgent`` subclass that wraps a deterministic
helper (or an LLM subroutine call). State threads through as a
``WakeState`` carried on a per-wake ``contextvars.ContextVar`` —
sub-agents read + mutate the same instance. Errors at any step
append to ``WakeState.errors`` rather than throwing — the wake
always closes cleanly via ``CloseAgent`` (Step 7), even if
intermediate steps fail.

## Why ``contextvars`` not ADK session-state

google-adk's ``InMemorySessionService`` deep-copies the session on
``get_session`` and at the runner boundary. If we put ``WakeState``
on ``ctx.session.state``, mutations made by Step 1 wouldn't be
visible to Step 2 — each agent would see its own copy.

The clean fix is to bind a ``WakeState`` reference on a context-var
at runner entry, have every sub-agent ``_WAKE_STATE.get()`` it, and
reset on exit. asyncio task scopes already isolate context-vars
between concurrent invocations, so the contract is the same as
session.state but without the deep-copy boundary.

## Per-step contracts

Each step lives in two places:

- The deterministic helper / LLM subroutine in
  ``alice_thinking.workflows.stage_b.steps`` (sometimes calling out to
  ``subroutines`` for LLM judgement).
- The ADK ``BaseAgent`` wrapper in
  ``alice_thinking.workflows.stage_b.agents`` (looks up the body by
  step name in ``_STEP_TABLE``, runs it under a per-step timeout,
  emits one telemetry event).

That split keeps the bulk of the logic plain Python (testable
without ADK) while the agent layer stays minimal — all 7 step
wrappers share a single ``StageBStepAgent`` class that dispatches
through a name-keyed table.

## LLM subroutines

All LLM calls flow through a ``ModelCall`` seam (``Callable[[str, str],
Awaitable[str]]``). Tests inject a fake; production wires
``make_default_model_call`` which constructs a
``google.adk.models.lite_llm.LiteLlm`` adapter pointed at the local
Qwen endpoint (``http://10.20.30.177:8033/v1``, OpenAI-compatible).

The seam is the same one from
``alice_thinking.design_pipeline.SubAgentRunner`` — workflow code
never imports a concrete LLM client; tests pass a fake; production
wires LiteLlm at the runner edge.

## Side checks (Step 5)

Three branches run concurrently. The ADK structural shape is a
``ParallelAgent`` with three branch sub-agents, but the actual
fan-out is a single ``asyncio.gather`` inside ``steps.side_checks``
— that gives us a single per-branch timeout contract that's
straightforward to test. Each branch returns a ``SideCheckResult``;
errored / timed-out branches have ``ok=False, error="..."``.

## Telemetry — per-step

Every step emits a telemetry event:

```json
{
  "ts": "...",
  "type": "stage_b_step",
  "step": "drain_inbox",
  "duration_ms": 1400,
  "ok": true,
  "details": {"notes_processed": 3, "actions": ["PromoteToVault", "AppendToDaily", "Discard"]}
}
```

Per-step duration + outcome answers questions the prompt-driven
setup couldn't: "which step is the long pole?", "is grooming
completing or timing out?", "is drain_inbox getting starved by
side_checks?". Same observability win that PR #18 gave us for vault
metrics.

## Failure containment

Per-step errors append to ``WakeState.errors`` and the workflow
continues. This solves yesterday's hung-wake class — if
``groom_target`` hangs on a model call, the per-step timeout (60s
default) trips, the step records the error, and the wake closes
cleanly. No flock-holding zombie process. Stage 1 of the
wake-robustness design (outer timeout) is still the floor; per-step
timeouts give earlier resolution.

## Cutover

Behind two config flags (``thinking.stage_b_workflow_enabled``,
``thinking.stage_b_shadow_enabled``). Protocol +
events.jsonl-driven shadow comparison: ``stage-b-cutover.md``.

## What this isn't

- Not a full rewrite of thinking. Active mode and Stage D stay
  prompt-driven. Stage C gets the same treatment in a follow-up.
- Not a model swap. The LLM subroutines run on the same backend the
  rest of thinking uses (Qwen via the local endpoint today; cloud
  routed via per-stage backend if config says so).
- Not portable to Stage D synthesis. Stage D's generative
  recombination resists workflow shape.
- Not a skills system. ADK workflows are first-class agentic
  constructs in code; Anthropic skills are description-matched
  prompt artifacts. Different tools for different problems.
