"""ConsolidationStage (Stage B) — sleep-mode default sub-stage.

Phase 3 implementation: identical kernel spec + prompt as
:class:`ActiveMode`. The point of Phase 3 is the *shape* — the
selector dispatches to a Stage object that the kernel adapter
treats like any other mode. Phase 4 differentiates the prompt
body (Stage B-specific operations: inbox drain, link audit, etc.).

The class name appears in the ``mode`` field of ``wake_start`` /
``wake_end`` events as ``"sleep:consolidate"`` so the viewer can
attribute behavior even before Phase 4 lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice_core.kernel import KernelSpec

from ..base import WakeContext, _NullPostRun


if TYPE_CHECKING:
    from alice_core.kernel import KernelResult


class ConsolidationStage(_NullPostRun):
    """Stage B: vault consolidation. Phase 3 stub mirrors ActiveMode."""

    name = "sleep:consolidate"

    def kernel_spec(self, ctx: WakeContext) -> KernelSpec:
        return KernelSpec(
            model=ctx.model,
            allowed_tools=list(ctx.tools),
            cwd=ctx.cwd,
            add_dirs=ctx.add_dirs,
            max_seconds=ctx.max_seconds,
            thinking="medium",
            append_system_prompt=ctx.system_prompt or None,
        )

    async def build_prompt(self, ctx: WakeContext) -> str:
        # Phase 5: load via the stage-specific template name. The
        # template currently extends thinking.wake.active so behavior
        # is unchanged; Phase 4 (deferred) swaps in Stage B-specific
        # body without touching this code.
        if ctx.quick:
            from alice_prompts import load as load_prompt

            return load_prompt("thinking.quick")
        if ctx.inline_prompt:
            return ctx.inline_prompt
        from ..._prompt_assembly import build_wake_prompt

        return build_wake_prompt(
            "thinking.wake.sleep.consolidate",
            now=ctx.now,
            directive_path=ctx.directive_path,
        )
