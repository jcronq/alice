# Stage B cutover — flag flip + shadow comparison

> **status:** active. Companion to ``stage-b-adk-workflow-sketch.md``.
> Describes the operator-facing protocol for moving Stage B
> (Consolidation) wakes from the prompt-driven path (``sleep-b.md`` +
> LLM agent loop) to the google-adk SequentialAgent workflow at
> ``alice_thinking.workflows.stage_b``.

## Configuration knobs

Two flags in ``alice.config.json`` under the ``thinking`` block:

```json
{
  "thinking": {
    "stage_b_workflow_enabled": false,
    "stage_b_shadow_enabled":  false,
    "stage_b_step_timeouts": {
      "drain_inbox": 60,
      "groom_target": 60,
      "side_checks": 60
    }
  }
}
```

- ``stage_b_workflow_enabled`` (bool, default ``false``) — when ``true`` and
  the wake routes to ``Phase.SLEEP_B``, the wake skips the prompt-driven
  ``SleepMode`` path and runs the typed-step workflow instead. The
  ``sleep-b.md`` fragment is no longer read by the kernel.
- ``stage_b_shadow_enabled`` (bool, default ``false``) — when ``true`` AND
  ``stage_b_workflow_enabled=false``, the workflow runs in shadow mode
  alongside the prompt-driven path. Shadow runs use ``apply_writes=False``
  (no filesystem changes) and tag telemetry with the ``stage_b_shadow_*``
  prefix so the viewer can filter shadow events from real ones.
- ``stage_b_step_timeouts`` (dict, optional) — per-step timeout overrides
  in seconds. Unknown keys are ignored. Defaults are LLM steps 60s, sync
  steps 5s.

## Cutover protocol

1. **Initial state.** Both flags ``false``. Existing prompt path runs Stage B
   unchanged. The workflow code is loaded but never invoked.

2. **Enable shadow.** Set ``stage_b_shadow_enabled: true``, leave
   ``stage_b_workflow_enabled: false``. On every Stage B wake the prompt
   path runs as before AND the workflow runs in shadow with no writes. Both
   emit telemetry; shadow events are tagged ``stage_b_shadow_step`` /
   ``stage_b_shadow_wake_summary``.

3. **Compare outputs.** Tail ``memory/events.jsonl`` (or use
   ``alice-viewer``) over a few wakes and diff:

   - Inbox actions: workflow output should be a strict subset of prompt
     output. The workflow may classify a note differently (e.g. promote
     vs. discard); the absolute volume should be similar.
   - Grooming target picked: the deterministic scoring should converge to
     the same target the prompt picked (or one with the same score and
     a lex-tiebreak choice).
   - Side-checks fired: matching counts of stale_finding_lint /
     shadow_neighbor / conflict_scan invocations.

   Mismatch is fine if it's an improvement — the workflow's whole point
   is to drop ad-hoc bash and off-by-one filename parsing. Mismatch is a
   blocker if the workflow misses work the prompt path was doing.

4. **Flip the cutover.** Set ``stage_b_workflow_enabled: true``. The
   prompt path stops running on Stage B wakes. Shadow flag becomes a
   no-op when workflow is enabled. ``sleep-b.md`` stays in the repo as
   documentation but is no longer read by the kernel.

5. **Rollback path.** Flip ``stage_b_workflow_enabled`` back to ``false``.
   Existing prompt path resumes immediately on the next wake. No state to
   migrate; the workflow's filesystem outputs (vault writes, daily
   appends, surface emissions) are the same shape the prompt path
   produces.

## Telemetry surface

Per-step events:

```json
{
  "ts": 1746729600.0,
  "event": "stage_b_step",
  "step": "drain_inbox",
  "duration_ms": 1400,
  "ok": true,
  "details": {"notes_processed": 3, "actions": ["PromoteToVault", "AppendToDaily", "Discard"]}
}
```

Wake summary:

```json
{
  "ts": 1746729612.3,
  "event": "stage_b_wake_summary",
  "duration_ms": 12300,
  "steps_ok": 7,
  "steps_failed": 0,
  "actions_total": 5,
  "surfaces_emitted": 1
}
```

Shadow runs use the ``stage_b_shadow_step`` / ``stage_b_shadow_wake_summary``
event names so they're filterable from live writes.

## Failure containment

Each step has a per-step timeout (configurable via
``stage_b_step_timeouts``). On timeout or exception, the step records a
:class:`StepError` on ``WakeState.errors`` and the workflow continues to
the next step. The wake **always** closes via Step 7
(``CloseAgent``) and emits a wake summary, even if every prior step
failed — that's the contract that prevents the flock-zombie regression.

## Where the code lives

- ``alice_thinking/workflows/stage_b/__init__.py`` — public entry
  points (``run_stage_b_wake``, ``run_stage_b_shadow``).
- ``alice_thinking/workflows/stage_b/types.py`` — ``WakeState``,
  ``Action`` union, ``Diff`` types, side-check + surface payloads.
- ``alice_thinking/workflows/stage_b/scoring.py`` — deterministic
  candidate scoring for ``pick_grooming_target``.
- ``alice_thinking/workflows/stage_b/subroutines.py`` — the LLM-calling
  wrappers (``classify_and_route_note``, ``produce_grooming_diff``, +
  the three side-check subroutines). All take a :class:`ModelCall`
  callable as the seam tests inject; production wires
  ``make_default_model_call`` which dispatches through google-adk's
  ``LiteLlm`` adapter to the local Qwen endpoint
  (``http://10.20.30.177:8033/v1``, OpenAI-compatible).
- ``alice_thinking/workflows/stage_b/steps.py`` — the seven step
  bodies plus deterministic apply helpers (``apply_action``,
  ``apply_diff``, ``consume_note``).
- ``alice_thinking/workflows/stage_b/agents.py`` — google-adk
  ``BaseAgent`` wrappers for each step + the ``SequentialAgent`` /
  ``ParallelAgent`` composition. Per-step error containment, timeouts,
  and telemetry emission live in :func:`_run_step` (one shared seam
  across all seven sub-agents).
- ``alice_thinking/workflows/stage_b/runner.py`` — public entry
  points: builds the in-memory ADK session, binds the per-wake context
  vars, drives ``Runner.run_async``, finalizes the
  :class:`WakeSummary` from the contextvar-bound state.
- ``alice_thinking/workflows/stage_b/prompts/`` — one ``.md`` fragment
  per LLM subroutine. Loaded as package resources at runtime.

The workflow is composed via google-adk's workflow primitives
(``SequentialAgent``, ``ParallelAgent``, ``BaseAgent``); LLM calls
flow through ``google.adk.models.lite_llm.LiteLlm`` to the local Qwen
endpoint. Per-wake state is carried via a ``contextvars.ContextVar``
rather than the ADK in-memory session-state dict — the deep-copy
boundary of the in-memory session service would otherwise lose the
shared :class:`WakeState` references between sub-agents.
