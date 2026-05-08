"""Stage B workflow runner — per-step timeouts, telemetry, error containment.

The runner threads :class:`WakeState` through the seven steps in
:mod:`.steps`, applying per-step timeouts and emitting one telemetry
event per step plus a wake-level summary at the end. Errors at any
step are contained — the wake always closes via Step 7 even if
intermediate steps fail.

Two entry points:

- :func:`run_stage_b_wake` — production entry. Takes a config + the
  wake context, dispatches through the kernel for LLM calls, writes
  to the filesystem.
- :func:`run_stage_b_shadow` — shadow-mode entry. Same code path, but
  ``apply_writes=False`` everywhere — no filesystem writes happen, and
  telemetry is tagged ``stage_b_shadow_*`` so the cutover comparison
  can distinguish shadow runs from real runs.

The runner does NOT compose prompts or pick a Phase — those still
live in the existing ``alice_thinking.runtime.PhaseRunner`` /
``alice_thinking.modes.sleep.SleepMode`` plumbing. ``run_stage_b_wake``
is invoked from the wake dispatch in :mod:`alice_thinking.wake` when
``thinking.stage_b_workflow_enabled=true``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from alice_core.events import EventEmitter, EventLogger

from .steps import (
    close as step_close,
    drain_inbox as step_drain_inbox,
    emit_surfaces as step_emit_surfaces,
    groom_target as step_groom_target,
    pick_grooming_target as step_pick_grooming_target,
    read_wake_state as step_read_wake_state,
    side_checks as step_side_checks,
)
from .subroutines import ModelCall, make_default_model_call
from .types import StepError, StepResult, WakeState, WakeSummary


__all__ = [
    "StageBRunnerConfig",
    "DEFAULT_STEP_TIMEOUTS",
    "run_stage_b_wake",
    "run_stage_b_shadow",
    "load_runner_config",
]


# Per-step timeout defaults (seconds). LLM-calling steps: 60s. Pure
# deterministic steps: 5s. Side-checks: 60s outer (each branch has its
# own 30s timeout inside ``step_side_checks``).
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
    """Per-wake configuration for the Stage B workflow runner.

    ``shadow_mode=True`` flips ``apply_writes=False`` everywhere so the
    workflow runs without touching disk. Use for cutover validation
    (compare shadow output to live prompt-driven output before flipping
    the cutover flag).
    """

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
    """Build a :class:`StageBRunnerConfig` with overrides from
    ``alice.config.json thinking.stage_b_step_timeouts``.

    Unknown keys in the override dict are ignored so configs can ship
    ahead of the code consuming them.
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


# Type aliases for the step seam — async or sync. We wrap sync steps in
# a trivial coroutine to give every step the same shape.
StepFn = Callable[..., Awaitable[Any]]


async def _run_step(
    *,
    name: str,
    coro: Awaitable[Any],
    timeout_s: float,
    state: WakeState,
    emitter: EventEmitter,
    event_prefix: str,
    details_factory: Callable[[Any], dict[str, Any]],
) -> tuple[Optional[Any], StepResult]:
    """Run one step under a timeout. Append errors to state.errors,
    emit telemetry, return ``(value, StepResult)``.
    """
    started = time.time()
    try:
        value = await asyncio.wait_for(coro, timeout=timeout_s)
        ok = True
        error: Optional[str] = None
    except asyncio.TimeoutError:
        value = None
        ok = False
        error = "timeout"
        state.errors.append(
            StepError(step=name, error_type="timeout", message=f"exceeded {timeout_s}s")
        )
    except Exception as exc:  # noqa: BLE001
        value = None
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        state.errors.append(
            StepError(step=name, error_type="exception", message=str(exc))
        )
    duration_ms = int((time.time() - started) * 1000)
    try:
        details = details_factory(value) if ok else {}
    except Exception:  # noqa: BLE001
        details = {}
    result = StepResult(
        step=name,
        ok=ok,
        duration_ms=duration_ms,
        details=details,
        error=error,
    )
    emitter.emit(
        f"{event_prefix}_step",
        step=name,
        duration_ms=duration_ms,
        ok=ok,
        details=details,
        error=error,
    )
    return value, result


async def _async_wrap(value: Any) -> Any:
    """Wrap a sync return value into an awaitable for ``_run_step``."""
    return value


async def run_stage_b_wake(
    config: StageBRunnerConfig,
    *,
    model_call: Optional[ModelCall] = None,
    emitter: Optional[EventEmitter] = None,
) -> WakeSummary:
    """Run one Stage B wake through the typed workflow.

    ``model_call`` defaults to a kernel-backed real implementation;
    tests inject a fake. ``emitter`` defaults to a JSONL EventLogger
    over ``config.event_log_path`` (or ``memory/events.jsonl`` under
    the mind dir, mirroring the existing thinking telemetry stream).
    """
    if emitter is None:
        log_path = config.event_log_path or (
            config.mind_dir / "memory" / "events.jsonl"
        )
        emitter = EventLogger(log_path)

    if model_call is None:
        # Production: dispatch through the kernel using the model name
        # the rest of thinking is using.
        from alice_core.config.model import load as load_model_config

        model_config = load_model_config(config.mind_dir)
        model_call = make_default_model_call(
            model=model_config.thinking.model or "claude-sonnet-4-6",
            backend=model_config.thinking,
        )

    apply_writes = not config.shadow_mode
    event_prefix = "stage_b_shadow" if config.shadow_mode else "stage_b"

    wake_started = time.time()

    # Step 1 — read_wake_state.
    state, step1 = await _run_step(
        name="read_wake_state",
        coro=_async_wrap(
            step_read_wake_state(
                mind_dir=config.mind_dir,
                state_dir=config.state_dir,
                wake_file_path=config.wake_file_path,
                now=config.now,
            )
        ),
        timeout_s=config.step_timeouts["read_wake_state"],
        # We don't have a state yet — synthesize a minimal placeholder
        # to record errors against. Real wake replaces this on success.
        state=WakeState(
            mind_dir=config.mind_dir,
            state_dir=config.state_dir,
            wake_file_path=config.wake_file_path,
            mode="sleep_b",
            now=config.now or _dt.datetime.now(),
        ),
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda s: {
            "inbox_files": len(s.inbox_files),
            "mode": s.mode,
        },
    )
    results: list[StepResult] = [step1]
    if state is None:
        # Read failed catastrophically — close out with a synthetic
        # state so the wake still emits a summary.
        state = WakeState(
            mind_dir=config.mind_dir,
            state_dir=config.state_dir,
            wake_file_path=config.wake_file_path,
            mode="sleep_b",
            now=config.now or _dt.datetime.now(),
        )

    # Step 2 — drain_inbox.
    inbox_result, step2 = await _run_step(
        name="drain_inbox",
        coro=step_drain_inbox(
            state,
            model_call=model_call,
            vault_index=config.vault_index,
            apply_writes=apply_writes,
        ),
        timeout_s=config.step_timeouts["drain_inbox"],
        state=state,
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda r: {
            "notes_processed": len(r.actions),
            "actions": [type(a).__name__ for a in r.actions],
            "consumed": len(r.consumed_paths),
            "errors": len(r.per_note_errors),
        },
    )
    results.append(step2)

    # Step 3 — pick_grooming_target.
    target, step3 = await _run_step(
        name="pick_grooming_target",
        coro=_async_wrap(step_pick_grooming_target(state)),
        timeout_s=config.step_timeouts["pick_grooming_target"],
        state=state,
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda t: {"target": str(t) if t else None},
    )
    results.append(step3)

    # Step 4 — groom_target.
    diff, step4 = await _run_step(
        name="groom_target",
        coro=step_groom_target(
            state,
            target,
            model_call=model_call,
            vault_index=config.vault_index,
            apply_writes=apply_writes,
        ),
        timeout_s=config.step_timeouts["groom_target"],
        state=state,
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda d: {
            "applied": d is not None,
            "rationale": (d.rationale if d is not None else None),
            "fm_changes": (len(d.frontmatter_changes) if d is not None else 0),
            "wikilink_fixes": (len(d.wikilink_fixes) if d is not None else 0),
            "section_edits": (len(d.section_edits) if d is not None else 0),
        },
    )
    results.append(step4)

    # Step 5 — side_checks.
    side_results, step5 = await _run_step(
        name="side_checks",
        coro=step_side_checks(
            state,
            target,
            model_call=model_call,
            branch_timeout_s=config.side_check_branch_timeout_s,
            apply_writes=apply_writes,
        ),
        timeout_s=config.step_timeouts["side_checks"],
        state=state,
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda r: {
            "branches": [
                {
                    "name": br.name,
                    "ok": br.ok,
                    "summary": br.action_summary,
                    "error": br.error,
                }
                for br in (r.all() if r is not None else [])
            ]
        },
    )
    results.append(step5)

    # Step 6 — emit_surfaces.
    surfaces_count, step6 = await _run_step(
        name="emit_surfaces",
        coro=_async_wrap(step_emit_surfaces(state, apply_writes=apply_writes)),
        timeout_s=config.step_timeouts["emit_surfaces"],
        state=state,
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda c: {"count": int(c or 0)},
    )
    results.append(step6)

    # Step 7 — close.
    duration_ms_so_far = int((time.time() - wake_started) * 1000)
    summary, step7 = await _run_step(
        name="close",
        coro=_async_wrap(
            step_close(
                state,
                results,
                duration_ms=duration_ms_so_far,
                apply_writes=apply_writes,
                run_prune=apply_writes,
            )
        ),
        timeout_s=config.step_timeouts["close"],
        state=state,
        emitter=emitter,
        event_prefix=event_prefix,
        details_factory=lambda s: {
            "summary_path": str(s.summary_path) if s and s.summary_path else None,
        },
    )
    results.append(step7)

    # Final wake summary event.
    total_duration_ms = int((time.time() - wake_started) * 1000)
    if summary is None:
        summary = WakeSummary(
            steps=list(results),
            actions_total=len(state.inbox_actions),
            surfaces_emitted=int(surfaces_count or 0),
            duration_ms=total_duration_ms,
            errors=list(state.errors),
            summary_path=None,
        )
    else:
        # Re-stamp duration_ms with the final value (Step 7 was given a
        # snapshot before its own duration was known).
        summary = dataclasses.replace(summary, duration_ms=total_duration_ms)

    emitter.emit(
        f"{event_prefix}_wake_summary",
        duration_ms=total_duration_ms,
        steps_ok=summary.steps_ok,
        steps_failed=summary.steps_failed,
        actions_total=summary.actions_total,
        surfaces_emitted=summary.surfaces_emitted,
    )
    return summary


async def run_stage_b_shadow(
    config: StageBRunnerConfig,
    *,
    model_call: Optional[ModelCall] = None,
    emitter: Optional[EventEmitter] = None,
) -> WakeSummary:
    """Shadow-mode entry — runs the workflow with ``apply_writes=False``.

    Telemetry is tagged ``stage_b_shadow_*`` so the cutover comparison
    can distinguish shadow output from live writes. The shadow mode is
    a no-op for the filesystem; it returns a :class:`WakeSummary`
    describing what the workflow WOULD have done.
    """
    shadow_config = dataclasses.replace(config, shadow_mode=True)
    return await run_stage_b_wake(shadow_config, model_call=model_call, emitter=emitter)
