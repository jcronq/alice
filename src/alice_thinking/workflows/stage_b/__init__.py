"""Stage B (Consolidation) — google-adk-driven workflow.

Replaces the prompt-driven Stage B path (``sleep-b.md`` + free LLM
agent loop) with a typed-step graph composed of google-adk
``SequentialAgent`` / ``ParallelAgent`` sub-agents. LLM judgement is
contained in five small subroutines (one per ``prompts/<name>.md``
fragment); everything else is deterministic.

Public entry points:

- :func:`run_stage_b_wake` — production entry. Dispatches LLM calls
  via google-adk's ``LiteLlm`` adapter to the local Qwen endpoint
  (``http://10.20.30.177:8033/v1``, OpenAI-compatible).
- :func:`run_stage_b_shadow` — shadow-mode entry. ``apply_writes=False``
  everywhere; telemetry tagged ``stage_b_shadow_*`` so cutover
  comparison can filter shadow events from real ones.

Cutover gated by ``thinking.stage_b_workflow_enabled`` in
``alice.config.json``. Cutover protocol: ``docs/designs/stage-b-cutover.md``.
Design sketch: ``docs/designs/stage-b-adk-workflow-sketch.md``.
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
