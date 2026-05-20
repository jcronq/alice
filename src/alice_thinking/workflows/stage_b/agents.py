"""ADK agent wrappers for the Stage B workflow.

Each step is a tiny :class:`google.adk.agents.BaseAgent` subclass that
wraps a deterministic helper from :mod:`steps`. The seven steps are
composed into one :class:`SequentialAgent`; Step 5 (side checks) is a
:class:`ParallelAgent` over three structural-mirror branch agents
(the actual fan-out runs through :func:`steps.side_checks` for a
single per-branch timeout contract).

Per-wake state lives in a :class:`contextvars.ContextVar` that the
runner sets up before driving ``agent.run_async`` and tears down
after — every sub-agent within the SequentialAgent reads + mutates
the same :class:`WakeState` via the contextvar. (We don't use ADK's
``InvocationContext.session.state`` because the in-memory session
service deep-copies state across the runner boundary; a contextvar
gives us live shared references with the same task-scope isolation.)

Per-step error containment + timeouts + telemetry all live in
:func:`_run_step`. Anything an inner step raises becomes a
:class:`StepError` on the WakeState plus a failed :class:`StepResult`
in ``state.step_results``. The wake always closes via the final
``CloseAgent``.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from google.adk.agents import BaseAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types as gtypes

from core.events import EventEmitter

from . import steps as _steps
from .types import StepError, StepResult, WakeState


__all__ = [
    "build_stage_b_agent",
    "StageBAgents",
    "current_wake_state",
    "current_runner_config",
    "bind_run_context",
    "reset_run_context",
    "CONFIG_KEY",
]


CONFIG_KEY = "stage_b_config"


# ---------------------------------------------------------------------------
# Per-wake context — bound by the runner, read inside each agent. Live
# shared refs sidestep ADK's session-state deep-copy boundary.
# ---------------------------------------------------------------------------


_WAKE_STATE: contextvars.ContextVar[WakeState] = contextvars.ContextVar(
    "stage_b_wake_state"
)
_RUN_CFG: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "stage_b_run_cfg"
)


def current_wake_state() -> WakeState:
    return _WAKE_STATE.get()


def current_runner_config() -> dict[str, Any]:
    return _RUN_CFG.get()


def bind_run_context(state: WakeState, cfg: dict[str, Any]) -> tuple[
    contextvars.Token[WakeState], contextvars.Token[dict[str, Any]]
]:
    return _WAKE_STATE.set(state), _RUN_CFG.set(cfg)


def reset_run_context(
    tokens: tuple[
        contextvars.Token[WakeState], contextvars.Token[dict[str, Any]]
    ],
) -> None:
    state_tok, cfg_tok = tokens
    _WAKE_STATE.reset(state_tok)
    _RUN_CFG.reset(cfg_tok)


# ---------------------------------------------------------------------------
# Step driver — error containment + timeout + telemetry. One seam shared
# by all seven steps.
# ---------------------------------------------------------------------------


async def _run_step(
    *,
    name: str,
    body: Callable[[], Awaitable[Any]],
    details_factory: Callable[[Any], dict[str, Any]],
) -> Any:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    timeout_s = cfg["step_timeouts"].get(name, 60.0)
    emitter: EventEmitter = cfg["emitter"]
    event_prefix: str = cfg["event_prefix"]

    started = time.time()
    value: Any = None
    ok = True
    error: str | None = None
    try:
        value = await asyncio.wait_for(body(), timeout=timeout_s)
    except asyncio.TimeoutError:
        ok = False
        error = "timeout"
        state.errors.append(
            StepError(step=name, error_type="timeout", message=f"exceeded {timeout_s}s")
        )
    except Exception as exc:  # noqa: BLE001
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
    state.step_results.append(
        StepResult(
            step=name, ok=ok, duration_ms=duration_ms, details=details, error=error
        )
    )
    emitter.emit(
        f"{event_prefix}_step",
        step=name,
        duration_ms=duration_ms,
        ok=ok,
        details=details,
        error=error,
    )
    return value


def _empty_event(author: str) -> Event:
    """Yield-once placeholder so each step satisfies AsyncGenerator's
    "must yield at least one event" contract."""
    return Event(
        author=author,
        invocation_id="",
        content=gtypes.Content(role="model", parts=[]),
    )


# ---------------------------------------------------------------------------
# Step bodies — closures over the contextvar-bound state + cfg.
# ---------------------------------------------------------------------------


async def _do_read_wake_state() -> WakeState:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    fresh = _steps.read_wake_state(
        mind_dir=cfg["mind_dir"],
        state_dir=cfg["state_dir"],
        wake_file_path=cfg["wake_file_path"],
        now=cfg["now"],
        apply_writes=not cfg["shadow_mode"],
    )
    # Mutate the contextvar's WakeState in place so downstream agents
    # see the freshly-read fields (the contextvar can't be re-bound
    # mid-step without losing the chain of references).
    state.mode = fresh.mode
    state.now = fresh.now
    state.inbox_files = fresh.inbox_files
    state.vault_health = fresh.vault_health
    state.active_thread = fresh.active_thread
    state.apply_writes = fresh.apply_writes
    return state


async def _do_drain_inbox() -> Any:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    return await _steps.drain_inbox(
        state,
        model_call=cfg["model_call"],
        vault_index=cfg["vault_index"],
        apply_writes=not cfg["shadow_mode"],
    )


async def _do_pick_grooming_target() -> Any:
    return _steps.pick_grooming_target(_WAKE_STATE.get())


async def _do_groom_target() -> Any:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    return await _steps.groom_target(
        state,
        state.grooming_target,
        model_call=cfg["model_call"],
        vault_index=cfg["vault_index"],
        apply_writes=not cfg["shadow_mode"],
    )


async def _do_side_checks() -> Any:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    return await _steps.side_checks(
        state,
        state.grooming_target,
        model_call=cfg["model_call"],
        branch_timeout_s=cfg["side_check_branch_timeout_s"],
        apply_writes=not cfg["shadow_mode"],
    )


async def _do_emit_surfaces() -> Any:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    return _steps.emit_surfaces(state, apply_writes=not cfg["shadow_mode"])


async def _do_close() -> Any:
    state = _WAKE_STATE.get()
    cfg = _RUN_CFG.get()
    duration_ms = int((time.time() - cfg["wake_started"]) * 1000)
    summary = _steps.close(
        state,
        list(state.step_results),
        duration_ms=duration_ms,
        apply_writes=not cfg["shadow_mode"],
        run_prune=not cfg["shadow_mode"],
    )
    cfg["partial_holder"]["summary"] = summary
    return summary


# ---------------------------------------------------------------------------
# Per-step BaseAgent subclasses — minimal: name + body + details factory.
# ---------------------------------------------------------------------------


def _details_inbox(r: Any) -> dict[str, Any]:
    if r is None:
        return {}
    return {
        "notes_processed": len(r.actions),
        "actions": [type(a).__name__ for a in r.actions],
        "consumed": len(r.consumed_paths),
        "errors": len(r.per_note_errors),
    }


def _details_groom(d: Any) -> dict[str, Any]:
    return {
        "applied": d is not None,
        "rationale": d.rationale if d is not None else None,
        "fm_changes": len(d.frontmatter_changes) if d is not None else 0,
        "wikilink_fixes": len(d.wikilink_fixes) if d is not None else 0,
        "section_edits": len(d.section_edits) if d is not None else 0,
    }


def _details_side(r: Any) -> dict[str, Any]:
    return {
        "branches": [
            {"name": b.name, "ok": b.ok, "summary": b.action_summary, "error": b.error}
            for b in (r.all() if r is not None else [])
        ]
    }


# Mapping from step-name → (body coroutine factory, details factory).
_STEP_TABLE: dict[str, tuple[Callable[[], Awaitable[Any]], Callable[[Any], dict[str, Any]]]] = {
    "read_wake_state": (
        _do_read_wake_state,
        lambda v: {} if v is None else {"inbox_files": len(v.inbox_files), "mode": v.mode},
    ),
    "drain_inbox": (_do_drain_inbox, _details_inbox),
    "pick_grooming_target": (
        _do_pick_grooming_target,
        lambda t: {"target": str(t) if t else None},
    ),
    "groom_target": (_do_groom_target, _details_groom),
    "side_checks": (_do_side_checks, _details_side),
    "emit_surfaces": (_do_emit_surfaces, lambda c: {"count": int(c or 0)}),
    "close": (
        _do_close,
        lambda s: {"summary_path": str(s.summary_path) if s and s.summary_path else None},
    ),
}


class StageBStepAgent(BaseAgent):
    """Generic Stage B step — looks up its body + details by name."""

    step_name: str = ""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        body, details = _STEP_TABLE[self.step_name]
        await _run_step(name=self.step_name, body=body, details_factory=details)
        yield _empty_event(self.name)


class _SideCheckBranchAgent(BaseAgent):
    """Structural placeholder for the ``ParallelAgent`` mirror — the
    real fan-out runs through :func:`steps.side_checks` (one per-branch
    timeout, one telemetry event)."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        yield _empty_event(self.name)


# ---------------------------------------------------------------------------
# Composition — SequentialAgent containing seven step agents; Step 5 has
# a sibling ParallelAgent that mirrors the fan-out shape for graph
# introspection.
# ---------------------------------------------------------------------------


class StageBAgents:
    """Bundle of the assembled ADK agent tree — exposed for tests +
    runner introspection."""

    def __init__(self) -> None:
        def _step(name: str) -> StageBStepAgent:
            a = StageBStepAgent(name=name)
            a.step_name = name
            return a

        self.read_wake = _step("read_wake_state")
        self.drain = _step("drain_inbox")
        self.pick = _step("pick_grooming_target")
        self.groom = _step("groom_target")
        self.side_branches = ParallelAgent(
            name="side_check_branches",
            sub_agents=[
                _SideCheckBranchAgent(name="stale_finding_lint_branch"),
                _SideCheckBranchAgent(name="shadow_neighbor_branch"),
                _SideCheckBranchAgent(name="conflict_scan_branch"),
            ],
        )
        self.side = _step("side_checks")
        self.emit = _step("emit_surfaces")
        self.close = _step("close")

        self.root = SequentialAgent(
            name="stage_b_workflow",
            sub_agents=[
                self.read_wake,
                self.drain,
                self.pick,
                self.groom,
                self.side,
                self.emit,
                self.close,
            ],
        )


def build_stage_b_agent() -> StageBAgents:
    return StageBAgents()
