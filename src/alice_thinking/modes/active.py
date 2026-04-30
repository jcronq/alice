"""ActiveMode — the single mode that exists today.

Plan 03 Phase 2 codifies the existing single-mode behavior as a
:class:`Mode` implementation. Behavior unchanged: same prompt,
same kernel spec, same allowed tools.

Future phases introduce ``SleepMode`` (with Consolidation /
Downscaling / Recombination sub-stages); the selector picks
between them by hour + vault state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice_core.kernel import KernelSpec

from .base import Mode, WakeContext, _NullPostRun


if TYPE_CHECKING:
    from alice_core.kernel import KernelResult


class ActiveMode(_NullPostRun):
    """The 07:00–23:00 mode (and today's only mode).

    Reads the bootstrap template + injected directive (Plan 04
    Phase 6 wiring) and constructs the same KernelSpec the
    pre-refactor wake used inline.
    """

    name = "active"

    def kernel_spec(self, ctx: WakeContext) -> KernelSpec:
        return KernelSpec(
            model=ctx.model,
            allowed_tools=list(ctx.tools),
            cwd=ctx.cwd,
            max_seconds=ctx.max_seconds,
            # Adaptive thinking with summarized display so
            # ThinkingBlocks come back with non-empty text.
            thinking={"type": "adaptive", "display": "summarized"},
            # Plan 05 Phase 4: persona-rendered system prompt.
            # Empty string falls through as None-equivalent so the
            # kernel skips the system_prompt kwarg entirely.
            append_system_prompt=ctx.system_prompt or None,
        )

    async def build_prompt(self, ctx: WakeContext) -> str:
        if ctx.quick:
            from alice_prompts import load as load_prompt

            return load_prompt("thinking.quick")
        if ctx.inline_prompt:
            return ctx.inline_prompt
        from .._prompt_assembly import build_active_prompt

        return build_active_prompt(
            now=ctx.now,
            directive_path=ctx.directive_path,
        )
