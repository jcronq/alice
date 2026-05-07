"""Phase runtime — composes prompt + KernelSpec for a chosen Phase.

``PhaseRunner.run(phase, ctx)`` returns ``(prompt_text, KernelSpec)``.
The Mode protocol stays alive — :class:`alice_thinking.modes.ActiveMode`
and :class:`alice_thinking.modes.sleep.SleepMode` shrink to thin
wrappers that delegate here.

Phase 0 of the migration ships this layer with the sleep window
collapsed to ``Phase.SLEEP_B`` (matching today's behavior). Phase 3
flips ``PhaseConfig.enable_full_sleep_dispatch=True`` to unlock
B/C/D dispatch.

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


__all__ = ["PhaseRunner", "load_phase_config"]


# Per-phase soft tool allowlists. Phase 2 wires these into the kernel
# spec; Phase 1 keeps them as guidance only — the kernel still receives
# ``ctx.tools`` so behavior is identical to today. Stored here as the
# canonical reference so the matrix and the runtime stay in sync when
# Phase 2 lands.
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
    Phase.QUICK: (),
    Phase.DESIGN_COMMISSION: (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
    ),
}


def phase_default_allowed_tools(phase: Phase) -> list[str]:
    """Return the default allowlist for a phase. Reference only — the
    runtime currently honors ``ctx.tools`` (set from CLI/config) so
    Phase 0 ships behavior unchanged."""
    return list(_PHASE_TOOL_ALLOWLIST.get(phase, ()))


def load_phase_config(mind: pathlib.Path) -> PhaseConfig:
    """Resolve a :class:`PhaseConfig` from ``alice.config.json``.

    Reads the ``thinking.phase_routing`` block (if any) and applies it
    over the dataclass defaults. Unknown keys are ignored so configs
    can ship ahead of the code that consumes them.
    """
    cfg_path = mind / "config" / "alice.config.json"
    overrides: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            blob = json.loads(cfg_path.read_text())
        except (OSError, json.JSONDecodeError):
            blob = None
        if isinstance(blob, dict):
            think = blob.get("thinking") or {}
            block = think.get("phase_routing") or {}
            if isinstance(block, dict):
                overrides = block

    base = PhaseConfig()
    fields = {f for f in PhaseConfig.__dataclass_fields__}
    kwargs = {k: v for k, v in overrides.items() if k in fields}
    if not kwargs:
        return base
    # PhaseConfig is frozen; rebuild via dataclass replace semantics.
    from dataclasses import replace

    return replace(base, **kwargs)


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

        Phase 0/1: tool allowlist + ``max_seconds`` come from
        ``ctx.tools`` / ``ctx.max_seconds`` (CLI/config wired in
        :mod:`alice_thinking.wake`). Phase 2 swaps in the per-phase
        defaults from :data:`_PHASE_TOOL_ALLOWLIST` — this method is
        the single place that change lands.
        """
        return KernelSpec(
            model=ctx.model,
            allowed_tools=list(ctx.tools),
            cwd=ctx.cwd,
            add_dirs=ctx.add_dirs,
            max_seconds=ctx.max_seconds,
            thinking="medium",
            append_system_prompt=ctx.system_prompt or None,
        )

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
