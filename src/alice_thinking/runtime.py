"""Phase runtime — composes prompt + KernelSpec for a chosen Phase.

``PhaseRunner.run(phase, ctx)`` returns ``(prompt_text, KernelSpec)``.
The Mode protocol stays alive — :class:`alice_thinking.modes.ActiveMode`
and :class:`alice_thinking.modes.sleep.SleepMode` shrink to thin
wrappers that delegate here.

Phase 2 of the migration wires the per-phase tool allowlists
(:data:`_PHASE_TOOL_ALLOWLIST`) and ``max_seconds`` budgets
(:data:`_PHASE_MAX_SECONDS`) into the produced :class:`KernelSpec`.
Resolution order (highest precedence first) for both fields:

1. ``PhaseConfig.allowed_tools`` / ``PhaseConfig.max_seconds`` —
   set via ``alice.config.json thinking.phase_routing.*``.
2. ``ctx.tools`` / ``ctx.max_seconds`` — populated by
   ``alice_thinking.wake`` from the CLI ``--tools`` / ``--max-seconds``
   flags (or the legacy ``thinking.allowed_tools`` /
   ``thinking.max_wake_seconds`` config block).
3. Per-phase defaults from this module.

Phase 3 ships ``enable_full_sleep_dispatch=True`` so the cascade in
:func:`select_phase` fans sleep wakes out to B/C/D.

The :meth:`PhaseRunner._run_post_wake_hooks` extension point exists
as a no-op stub for the companion STM/LTM design — Hebbian
edge-weight updates plug in there. See §Required Interfaces for
Companion Designs in
``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``.
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
    "phase_default_max_seconds",
]


# Per-phase tool allowlists. Phase 2 wires these into the produced
# :class:`KernelSpec` as the *default* — config (``PhaseConfig``) and
# CLI (``ctx.tools``) overrides take precedence in :meth:`PhaseRunner.kernel_spec`.
# Names are Claude-style (matching ``WakeContext.tools``); PiKernel's
# ``_PI_TOOL_NAME_MAP`` translates them to pi-native names downstream.
_PHASE_TOOL_ALLOWLIST: dict[Phase, tuple[str, ...]] = {
    Phase.ACTIVE: (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
        "mcp__alice__send_message",
    ),
    Phase.SLEEP_B: (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
        "mcp__alice__send_message",
    ),
    # Stage C / D: vault-only maintenance + recombination — no Web*.
    Phase.SLEEP_C: (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "mcp__alice__send_message",
    ),
    Phase.SLEEP_D: (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "mcp__alice__send_message",
    ),
    # Quick smoke test — no tools.
    Phase.QUICK: (),
    # Design commission — pure design work, no Web*, no MCP.
    Phase.DESIGN_COMMISSION: (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
    ),
}


# Per-phase ``max_seconds`` defaults. ``0`` == unbounded. Quick keeps
# its 30-second smoke-test budget; the rest run unbounded by default
# and can be tightened via ``alice.config.json``.
_PHASE_MAX_SECONDS: dict[Phase, int] = {
    Phase.ACTIVE: 0,
    Phase.SLEEP_B: 0,
    Phase.SLEEP_C: 0,
    Phase.SLEEP_D: 0,
    Phase.QUICK: 30,
    Phase.DESIGN_COMMISSION: 0,
}


def phase_default_allowed_tools(phase: Phase) -> list[str]:
    """Return the default tool allowlist for ``phase`` (Claude-style names)."""
    return list(_PHASE_TOOL_ALLOWLIST.get(phase, ()))


def phase_default_max_seconds(phase: Phase) -> int:
    """Return the default ``max_seconds`` budget for ``phase``."""
    return _PHASE_MAX_SECONDS.get(phase, 0)


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

    def kernel_spec(self, phase: Phase, ctx: "WakeContext") -> KernelSpec:
        """Build a :class:`KernelSpec` for this phase + context.

        Phase 2: tool allowlist + ``max_seconds`` resolve in this
        precedence order:

        1. :class:`PhaseConfig` (``alice.config.json``).
        2. :class:`WakeContext` (CLI / legacy ``thinking.*``).
        3. The per-phase default (:data:`_PHASE_TOOL_ALLOWLIST`,
           :data:`_PHASE_MAX_SECONDS`).

        Quick mode short-circuits to its own (tools=[], max_seconds=30)
        so smoke-test wakes don't accidentally pick up the active
        allowlist.
        """
        if phase == Phase.QUICK or ctx.quick:
            return KernelSpec(
                model=ctx.model,
                allowed_tools=list(_PHASE_TOOL_ALLOWLIST[Phase.QUICK]),
                cwd=ctx.cwd,
                add_dirs=ctx.add_dirs,
                max_seconds=_PHASE_MAX_SECONDS[Phase.QUICK],
                thinking="medium",
                append_system_prompt=ctx.system_prompt or None,
            )

        return KernelSpec(
            model=ctx.model,
            allowed_tools=self._resolve_tools(phase, ctx),
            cwd=ctx.cwd,
            add_dirs=ctx.add_dirs,
            max_seconds=self._resolve_max_seconds(phase, ctx),
            thinking="medium",
            append_system_prompt=ctx.system_prompt or None,
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

    def _resolve_max_seconds(self, phase: Phase, ctx: "WakeContext") -> int:
        """Return the resolved ``max_seconds`` for ``phase`` + ``ctx``.

        ``0`` (or negative) at any layer == "fall through to the next
        layer." See :meth:`kernel_spec` for the precedence rules.
        """
        if self.config.max_seconds and self.config.max_seconds > 0:
            return self.config.max_seconds
        if ctx.max_seconds and ctx.max_seconds > 0:
            return ctx.max_seconds
        return phase_default_max_seconds(phase)

    def run(
        self,
        phase: Phase,
        ctx: "WakeContext",
        *,
        injected_content: Optional[str] = None,
    ) -> tuple[str, KernelSpec]:
        """Return ``(prompt_text, KernelSpec)`` for this phase + context."""
        prompt_text = self.build_prompt(
            phase, ctx, injected_content=injected_content
        )
        spec = self.kernel_spec(phase, ctx)
        return prompt_text, spec

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
