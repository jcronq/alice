"""Phase runtime — composes prompt + KernelSpec for a chosen Phase.

``PhaseRunner.run(phase, ctx)`` returns ``(prompt_text, KernelSpec)``.
The Mode protocol stays alive — :class:`alice_thinking.modes.ActiveMode`
and :class:`alice_thinking.modes.sleep.SleepMode` shrink to thin
wrappers that delegate here.

Tool allowlist resolution (post Speaking review 2026-05-07): every
phase except :attr:`Phase.QUICK` gets the same full tool set
(:data:`_FULL_TOOL_ALLOWLIST`). Per-phase tool restrictions were
removed — locking down web/MCP per phase narrowed the design space
unnecessarily; the prompt fragment guides use, not the harness.
Quick keeps an empty allowlist as the smoke-test sanity guard.

``max_seconds`` resolution: the wake interval is "fire at least
this often," not "kill after this long." There is no per-phase
``max_seconds`` default. Quick keeps the 30-second smoke-test
bound. Real phases default to ``0`` (unbounded) and honor only the
single user-facing knob ``thinking.max_wake_seconds`` (or
:class:`PhaseConfig.max_seconds`, which it feeds).

Resolution order (highest precedence first) for both fields:

1. ``PhaseConfig.allowed_tools`` / ``PhaseConfig.max_seconds`` —
   set via ``alice.config.json thinking.phase_routing.*`` or the
   convenience ``thinking.max_wake_seconds`` top-level key.
2. ``ctx.tools`` / ``ctx.max_seconds`` — populated by
   ``alice_thinking.wake`` from the CLI ``--tools`` / ``--max-seconds``
   flags (or the legacy ``thinking.allowed_tools`` /
   ``thinking.max_wake_seconds`` config block).
3. The full tool set (everywhere except Quick) and ``0`` for
   ``max_seconds`` (everywhere except Quick=30).

Phase 3 ships ``enable_full_sleep_dispatch=True`` so the cascade in
:func:`select_phase` fans sleep wakes out to B/C/D.

The :meth:`PhaseRunner._run_post_wake_hooks` extension point exists
as a no-op stub for the companion STM/LTM design — Hebbian
edge-weight updates plug in there. See §Required Interfaces for
Companion Designs in
``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``.

Per-issue phases (:attr:`Phase.PER_ISSUE_DESIGN` /
:attr:`Phase.PER_ISSUE_BUILD`) are stimulus-spawned by
:func:`alice_sm.dispatcher.spawn_thinking_agent` rather than picked by
the wake cadence selector. They route through the same
:meth:`PhaseRunner.run` path as cadence-driven phases — the caller is
the ``scripts/sm-thinking-perissue.py`` entrypoint, which reads the
spawn dir's ``prompt.txt`` and hands the issue body / approved design
note to the runner via ``injected_content``. The prompt fragments
(:mod:`alice_thinking.prompts.per-issue-design`, ``per-issue-build``)
carry per-issue-specific framing and bypass the wake prelude (see
:data:`alice_thinking.phase._PHASES_WITHOUT_PRELUDE`).
"""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, Any, Optional

from alice_core.kernel import KernelSpec

from ._prompt_assembly import wake_timestamp_header
from .phase import Phase, PhaseConfig, PromptFragmentLoader


if TYPE_CHECKING:
    from .modes.base import WakeContext


__all__ = [
    "PhaseRunner",
    "load_phase_config",
    "phase_default_allowed_tools",
    "QUICK_MAX_SECONDS",
]


# Single full tool allowlist shared by every non-Quick phase. Per
# Speaking's review (2026-05-07): per-phase restrictions narrowed
# the design space — a Stage D synthesis might want WebFetch for a
# citation, a Stage C cleanup might want MCP. The prompt fragment
# is the right place to guide use, not the harness. Names are
# Claude-style (matching ``WakeContext.tools``); PiKernel's
# ``_PI_TOOL_NAME_MAP`` translates them to pi-native names
# downstream.
#
# ``mcp__alice__run_experiment`` (2026-05-11): thinking-only tool that
# dispatches a sandboxed claude-CLI subagent. Wired in here so the
# kernel allowlist routes it through; the MCP server itself is
# composed in ``wake.py`` (it needs the emitter + auth from the wake
# context) and threaded onto the ``KernelSpec.mcp_servers`` field.
_FULL_TOOL_ALLOWLIST: tuple[str, ...] = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Grep",
    "Glob",
    "WebFetch",
    "WebSearch",
    "mcp__alice__send_message",
    "mcp__alice__run_experiment",
)


# Quick keeps its 30s smoke-test sanity bound. Real phases run
# unbounded unless the user pins ``thinking.max_wake_seconds``.
QUICK_MAX_SECONDS = 30


def phase_default_allowed_tools(phase: Phase) -> list[str]:
    """Return the default tool allowlist for ``phase`` (Claude-style names).

    Every non-Quick phase shares :data:`_FULL_TOOL_ALLOWLIST`. Quick
    is empty (no tools — the smoke test runs in /tmp).
    """
    if phase == Phase.QUICK:
        return []
    return list(_FULL_TOOL_ALLOWLIST)


def load_phase_config(mind: pathlib.Path) -> PhaseConfig:
    """Resolve a :class:`PhaseConfig` from ``alice.config.json``.

    Two override sources, in this order (later wins on conflict):

    1. The ``thinking`` block: ``enable_full_sleep_dispatch``
       (bool) and ``max_wake_seconds`` (int) — surfaced at the top
       level alongside the other ``thinking.*`` knobs (``model``,
       ``allowed_tools``, ...) so Jason can flip the kill-switch
       without nesting it under a sub-block.
    2. The ``thinking.phase_routing`` block: the canonical home for
       phase-routing tunables. Any field name on
       :class:`PhaseConfig` may be set here. Unknown keys are
       ignored so configs can ship ahead of the code consuming them.

    Phase-routing keys defined in both places resolve to the
    ``phase_routing`` value (the explicit block wins).
    """
    cfg_path = mind / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return PhaseConfig()
    try:
        blob = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return PhaseConfig()
    if not isinstance(blob, dict):
        return PhaseConfig()

    think = blob.get("thinking") or {}
    if not isinstance(think, dict):
        think = {}

    overrides: dict[str, Any] = {}

    # Top-level convenience overrides on the ``thinking`` block.
    if "enable_full_sleep_dispatch" in think and isinstance(
        think["enable_full_sleep_dispatch"], bool
    ):
        overrides["enable_full_sleep_dispatch"] = think["enable_full_sleep_dispatch"]
    if "max_wake_seconds" in think:
        try:
            overrides["max_seconds"] = int(think["max_wake_seconds"])
        except (TypeError, ValueError):
            pass

    # Canonical block — wins on conflict with the top-level keys.
    block = think.get("phase_routing") or {}
    if isinstance(block, dict):
        for k, v in block.items():
            overrides[k] = v

    fields = {f for f in PhaseConfig.__dataclass_fields__}
    kwargs = {k: v for k, v in overrides.items() if k in fields}
    if not kwargs:
        return PhaseConfig()
    from dataclasses import replace

    return replace(PhaseConfig(), **kwargs)


class PhaseRunner:
    """Composes the prompt + KernelSpec for a given :class:`Phase`.

    Stateful only insofar as it caches the loader. ``run()`` is the
    primary entry point used by ``wake.py``; modes wrap a call to
    ``run()`` for their respective phases.
    """

    def __init__(
        self,
        config: Optional[PhaseConfig] = None,
        loader: Optional[PromptFragmentLoader] = None,
    ) -> None:
        self.config = config or PhaseConfig()
        self.loader = loader or PromptFragmentLoader()

    # ------------------------------------------------------------------ #
    # Composition

    def build_prompt(
        self,
        phase: Phase,
        ctx: "WakeContext",
        *,
        injected_content: Optional[str] = None,
    ) -> str:
        """Compose the full prompt text for this phase + context.

        Quick mode and inline-prompt overrides are honored here so the
        Mode wrappers can stay thin. ``injected_content`` is the
        forward-compatible STM/LTM seam (no-op today).
        """
        if phase == Phase.QUICK or ctx.quick:
            from alice_prompts import load as load_prompt

            return load_prompt("thinking.quick")
        if ctx.inline_prompt:
            return ctx.inline_prompt
        header = wake_timestamp_header(ctx.now)
        return self.loader.compose(
            phase, timestamp_header=header, injected_content=injected_content
        )

    def kernel_spec(
        self,
        phase: Phase,
        ctx: "WakeContext",
        *,
        mcp_servers: Optional[dict[str, Any]] = None,
    ) -> KernelSpec:
        """Build a :class:`KernelSpec` for this phase + context.

        Tool allowlist + ``max_seconds`` resolve in this precedence
        order:

        1. :class:`PhaseConfig` (``alice.config.json``).
        2. :class:`WakeContext` (CLI / legacy ``thinking.*``).
        3. The runtime default — full tool set everywhere except
           Quick (empty), and ``0`` (unbounded) for ``max_seconds``
           everywhere except Quick (30s smoke-test bound).

        Quick mode short-circuits to its own (tools=[],
        max_seconds=30) so smoke-test wakes don't accidentally pick
        up the full allowlist.

        ``mcp_servers`` threads the thinking-side MCP server (built by
        :mod:`alice_thinking.tools`) into the spec. Quick mode skips
        MCP so smoke-test wakes stay minimal.
        """
        if phase == Phase.QUICK or ctx.quick:
            return KernelSpec(
                model=ctx.model,
                allowed_tools=[],
                cwd=ctx.cwd,
                add_dirs=ctx.add_dirs,
                max_seconds=QUICK_MAX_SECONDS,
                thinking="medium",
                append_system_prompt=ctx.system_prompt or None,
            )

        return KernelSpec(
            model=ctx.model,
            allowed_tools=self._resolve_tools(phase, ctx),
            cwd=ctx.cwd,
            add_dirs=ctx.add_dirs,
            max_seconds=self._resolve_max_seconds(ctx),
            thinking="medium",
            append_system_prompt=ctx.system_prompt or None,
            mcp_servers=mcp_servers,
        )

    def _resolve_tools(self, phase: Phase, ctx: "WakeContext") -> list[str]:
        """Return the resolved tool allowlist for ``phase`` + ``ctx``.

        See :meth:`kernel_spec` for the precedence rules.
        """
        if self.config.allowed_tools is not None:
            return list(self.config.allowed_tools)
        if ctx.tools:
            return list(ctx.tools)
        return phase_default_allowed_tools(phase)

    def _resolve_max_seconds(self, ctx: "WakeContext") -> int:
        """Return the resolved ``max_seconds`` for this real-phase wake.

        ``0`` (or negative) at any layer == "fall through to the next
        layer." Real phases run unbounded by default; Quick is
        handled before this method is reached.
        """
        if self.config.max_seconds and self.config.max_seconds > 0:
            return self.config.max_seconds
        if ctx.max_seconds and ctx.max_seconds > 0:
            return ctx.max_seconds
        return 0

    def run(
        self,
        phase: Phase,
        ctx: "WakeContext",
        *,
        injected_content: Optional[str] = None,
        mcp_servers: Optional[dict[str, Any]] = None,
    ) -> tuple[str, KernelSpec]:
        """Return ``(prompt_text, KernelSpec)`` for this phase + context.

        ``mcp_servers`` lets the wake-side glue inject thinking's
        per-wake MCP server (carrying the dispatch-time emitter + auth).
        Passed through to :meth:`kernel_spec`.
        """
        prompt_text = self.build_prompt(
            phase, ctx, injected_content=injected_content
        )
        spec = self.kernel_spec(phase, ctx, mcp_servers=mcp_servers)
        return prompt_text, spec

    # ------------------------------------------------------------------ #
    # Conflict resolution — task-type triggered, mirrors design commission.

    def _run_conflict_resolution(
        self, ctx: "WakeContext"
    ) -> dict[str, Any]:
        """Stub conflict-resolution dispatch.

        Mirrors the structural shape of
        :func:`alice_thinking.wake._run_commission` so the wake-side
        preempt hook can be wired today. The actual resolution
        logic (Sonnet review of vault contradictions, merge or fork
        the conflicting facts, archive into ``conflicts/.resolved/``)
        is deferred to a follow-up commit.

        Returns a no-op result dict with ``verdict="deferred"`` so
        callers can log telemetry and resolve the surface without
        committing fictitious work to the vault.
        """

        return {
            "phase": Phase.CONFLICT_RESOLUTION.value,
            "verdict": "deferred",
            "summary": (
                "Conflict resolution stub — real resolution logic deferred."
            ),
        }

    # ------------------------------------------------------------------ #
    # Companion-design extension point — STM/LTM Hebbian updates land here.

    async def _run_post_wake_hooks(self, ctx: "WakeContext", *, info: Optional[dict[str, Any]] = None) -> None:
        """No-op extension point invoked after Step 5 completes.

        The STM/LTM design (`cortex-memory/research/2026-05-07-thinking-stm-ltm-dual-substrate-design.md`)
        registers an edge-weight Hebbian updater here. Today this method
        is a no-op stub — no STM/LTM substrate exists yet.

        ``info`` is a forward-compatible dict the post-wake hook may
        consume (vault snapshot, wake statistics, handoff payload). The
        contract is intentionally loose; specifics ride with the
        STM/LTM design when it ships.
        """
        return None
