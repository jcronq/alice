"""Stage B workflow runner — drives the ADK SequentialAgent.

Two entry points:

- :func:`run_stage_b_wake` — production entry. Wires telemetry,
  builds the in-memory ADK session, runs the SequentialAgent, finalizes
  the :class:`WakeSummary`.
- :func:`run_stage_b_shadow` — same path with ``apply_writes=False``
  everywhere; telemetry tagged ``stage_b_shadow_*``.

Per-step error containment, per-step timeouts, and telemetry emission
all live inside :mod:`agents` — the runner only owns dispatch + final
summary assembly + the wake-level summary event.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from core.events import EventEmitter, EventLogger

from .agents import (
    bind_run_context,
    build_stage_b_agent,
    reset_run_context,
)
from .subroutines import ModelCall, make_default_model_call
from .types import WakeState, WakeSummary


__all__ = [
    "StageBRunnerConfig",
    "DEFAULT_STEP_TIMEOUTS",
    "run_stage_b_wake",
    "run_stage_b_shadow",
    "load_runner_config",
]


DEFAULT_STEP_TIMEOUTS: dict[str, float] = {
    "read_wake_state": 5.0,
    "drain_inbox": 60.0,
    "pick_grooming_target": 5.0,
    "groom_target": 60.0,
    "side_checks": 60.0,
    "emit_surfaces": 5.0,
    "close": 5.0,
}


@dataclass
class StageBRunnerConfig:
    mind_dir: pathlib.Path
    state_dir: pathlib.Path
    wake_file_path: Optional[pathlib.Path] = None
    now: Optional[_dt.datetime] = None
    step_timeouts: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_STEP_TIMEOUTS)
    )
    side_check_branch_timeout_s: float = 30.0
    shadow_mode: bool = False
    vault_index: Optional[dict[str, Any]] = None
    event_log_path: Optional[pathlib.Path] = None


def load_runner_config(
    *,
    mind_dir: pathlib.Path,
    state_dir: pathlib.Path,
    wake_file_path: Optional[pathlib.Path] = None,
    now: Optional[_dt.datetime] = None,
    shadow_mode: bool = False,
    event_log_path: Optional[pathlib.Path] = None,
    side_check_branch_timeout_s: float = 30.0,
) -> StageBRunnerConfig:
    """Build a :class:`StageBRunnerConfig` with timeout overrides from
    ``alice.config.json thinking.stage_b_step_timeouts``.
    """
    timeouts = dict(DEFAULT_STEP_TIMEOUTS)
    cfg_path = mind_dir / "config" / "alice.config.json"
    if cfg_path.is_file():
        try:
            blob = json.loads(cfg_path.read_text())
        except (OSError, json.JSONDecodeError):
            blob = {}
        think = (blob or {}).get("thinking") or {}
        if isinstance(think, dict):
            override = think.get("stage_b_step_timeouts") or {}
            if isinstance(override, dict):
                for k, v in override.items():
                    if k in timeouts:
                        try:
                            timeouts[k] = float(v)
                        except (TypeError, ValueError):
                            continue
    return StageBRunnerConfig(
        mind_dir=mind_dir,
        state_dir=state_dir,
        wake_file_path=wake_file_path,
        now=now,
        step_timeouts=timeouts,
        shadow_mode=shadow_mode,
        event_log_path=event_log_path,
        side_check_branch_timeout_s=side_check_branch_timeout_s,
    )


async def run_stage_b_wake(
    config: StageBRunnerConfig,
    *,
    model_call: Optional[ModelCall] = None,
    emitter: Optional[EventEmitter] = None,
) -> WakeSummary:
    """Run one Stage B wake through the ADK SequentialAgent.

    ``model_call`` defaults to :func:`make_default_model_call` (LiteLlm
    → local Qwen). Tests inject a fake. ``emitter`` defaults to a
    JSONL EventLogger over ``config.event_log_path`` (or
    ``memory/events.jsonl`` under the mind dir).
    """
    if emitter is None:
        log_path = config.event_log_path or (config.mind_dir / "memory" / "events.jsonl")
        emitter = EventLogger(log_path)
    if model_call is None:
        model_call = make_default_model_call()

    apply_writes = not config.shadow_mode
    event_prefix = "stage_b_shadow" if config.shadow_mode else "stage_b"

    # The shared per-wake state. Bound via contextvars so every
    # sub-agent reads + mutates the same instance — sidesteps the
    # in-memory session-service deep-copy boundary.
    state = WakeState(
        mind_dir=config.mind_dir,
        state_dir=config.state_dir,
        wake_file_path=config.wake_file_path,
        mode="sleep_b",
        now=config.now or _dt.datetime.now(),
        apply_writes=apply_writes,
    )
    partial_holder: dict[str, Any] = {}

    wake_started = time.time()
    runner_cfg = {
        "mind_dir": config.mind_dir,
        "state_dir": config.state_dir,
        "wake_file_path": config.wake_file_path,
        "now": config.now,
        "shadow_mode": config.shadow_mode,
        "model_call": model_call,
        "vault_index": config.vault_index,
        "step_timeouts": dict(config.step_timeouts),
        "side_check_branch_timeout_s": config.side_check_branch_timeout_s,
        "emitter": emitter,
        "event_prefix": event_prefix,
        "wake_started": wake_started,
        "partial_holder": partial_holder,
    }

    # Build the ADK runner with an in-memory session.
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.genai import types as gtypes

    agents = build_stage_b_agent()
    session_service = InMemorySessionService()
    app_name = "alice_stage_b"
    user_id = "thinking"
    session_id = f"stage_b_{uuid.uuid4().hex[:12]}"

    await session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id, state={}
    )

    adk_runner = Runner(
        app_name=app_name,
        agent=agents.root,
        session_service=session_service,
    )
    new_message = gtypes.Content(role="user", parts=[gtypes.Part.from_text(text="run")])

    tokens = bind_run_context(state, runner_cfg)
    try:
        async for _event in adk_runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
        ):
            pass
    finally:
        reset_run_context(tokens)

    final_state: WakeState = state
    partial: Optional[WakeSummary] = partial_holder.get("summary")

    total_duration_ms = int((time.time() - wake_started) * 1000)
    if partial is None:
        summary = WakeSummary(
            steps=list(final_state.step_results),
            actions_total=len(final_state.inbox_actions),
            surfaces_emitted=_extract_surfaces_count(final_state),
            duration_ms=total_duration_ms,
            errors=list(final_state.errors),
            summary_path=None,
        )
    else:
        summary = dataclasses.replace(
            partial,
            steps=list(final_state.step_results),
            duration_ms=total_duration_ms,
            errors=list(final_state.errors),
        )

    emitter.emit(
        f"{event_prefix}_wake_summary",
        duration_ms=total_duration_ms,
        steps_ok=summary.steps_ok,
        steps_failed=summary.steps_failed,
        actions_total=summary.actions_total,
        surfaces_emitted=summary.surfaces_emitted,
    )
    return summary


def _extract_surfaces_count(state: WakeState) -> int:
    for r in state.step_results:
        if r.step == "emit_surfaces":
            return int(r.details.get("count", 0) or 0)
    return 0


async def run_stage_b_shadow(
    config: StageBRunnerConfig,
    *,
    model_call: Optional[ModelCall] = None,
    emitter: Optional[EventEmitter] = None,
) -> WakeSummary:
    """Shadow-mode entry — runs with ``apply_writes=False``; telemetry
    tagged ``stage_b_shadow_*`` so cutover comparison can filter."""
    shadow_config = dataclasses.replace(config, shadow_mode=True)
    return await run_stage_b_wake(shadow_config, model_call=model_call, emitter=emitter)
