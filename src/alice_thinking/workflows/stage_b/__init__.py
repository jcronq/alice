"""Stage B (Consolidation) — typed-step workflow.

Lifts Stage B out of the prompt-driven LLM-orchestrator pattern (the
``sleep-b.md`` fragment + free LLM agent loop) into a native Python
workflow with typed steps, deterministic flow, and LLM subroutines for
parts that need judgement.

Public entry points:

- :func:`run_stage_b_wake` — production entry. Dispatches LLM calls
  through the kernel layer using the model.yml thinking backend.
- :func:`run_stage_b_shadow` — shadow-mode entry. Same code path, but
  ``apply_writes=False`` everywhere and telemetry tagged
  ``stage_b_shadow_*`` so cutover comparison can distinguish shadow
  runs from live runs.

The cutover is gated by ``thinking.stage_b_workflow_enabled`` in
``alice.config.json`` (default ``false``). Shadow runs can be wired in
parallel to the prompt-driven path while the flag is still false; once
the shadow output matches expectations, the operator flips the flag.

Design: ``docs/designs/stage-b-adk-workflow-sketch.md`` plus the
in-PR cutover doc.
"""

from .runner import (
    DEFAULT_STEP_TIMEOUTS,
    StageBRunnerConfig,
    load_runner_config,
    run_stage_b_shadow,
    run_stage_b_wake,
)
from .types import (
    Action,
    AppendToDaily,
    CreateConflictNote,
    Diff,
    Discard,
    FrontmatterChange,
    InboxResult,
    PromoteToVault,
    RouteToSurface,
    SectionEdit,
    SideCheckResult,
    SideCheckResults,
    StepError,
    StepResult,
    SurfacePayload,
    WakeState,
    WakeSummary,
    WikilinkFix,
)


__all__ = [
    "DEFAULT_STEP_TIMEOUTS",
    "StageBRunnerConfig",
    "load_runner_config",
    "run_stage_b_shadow",
    "run_stage_b_wake",
    "Action",
    "AppendToDaily",
    "CreateConflictNote",
    "Diff",
    "Discard",
    "FrontmatterChange",
    "InboxResult",
    "PromoteToVault",
    "RouteToSurface",
    "SectionEdit",
    "SideCheckResult",
    "SideCheckResults",
    "StepError",
    "StepResult",
    "SurfacePayload",
    "WakeState",
    "WakeSummary",
    "WikilinkFix",
]
