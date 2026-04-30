"""Sleep-mode dispatcher — Plan 03 Phase 3.

The sleep window (23:00–07:00 local) splits into three sub-stages
in the design:

- Stage B (Consolidation): inbox drain, link audit, frontmatter
  normalize, orphan linking.
- Stage C (Downscaling, NREM-3 / SWS analog): atomize large notes,
  archive stale dailies, merge duplicates, remove orphan stubs.
- Stage D (Recombination, REM analog): pick 2 recent research
  notes from different domains, look for unexpected connections,
  write a synthesis note.

Phase 3 ships :class:`SleepMode` as a thin stub that delegates to
:class:`ConsolidationStage` regardless of state — the most-defensible
default given today's behavior. Phase 4 (deferred per the combined
plan; behavior change requires shadow-running) wires the sub-stage
selector spelled out in ``inner/directive.md`` Step 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Mode, WakeContext, _NullPostRun
from .consolidate import ConsolidationStage


if TYPE_CHECKING:
    from alice_core.kernel import KernelResult, KernelSpec


__all__ = ["SleepMode", "ConsolidationStage"]


class SleepMode(_NullPostRun):
    """The 23:00–07:00 mode. Phase 3 always returns
    :class:`ConsolidationStage` (Stage B); Phase 4 wires the full
    sub-stage selector."""

    name = "sleep"

    def __init__(self) -> None:
        self._stage: Mode = ConsolidationStage()

    @property
    def stage(self) -> Mode:
        return self._stage

    def kernel_spec(self, ctx: WakeContext) -> "KernelSpec":
        return self._stage.kernel_spec(ctx)

    async def build_prompt(self, ctx: WakeContext) -> str:
        return await self._stage.build_prompt(ctx)

    async def post_run(self, ctx: WakeContext, result: "KernelResult") -> None:
        await self._stage.post_run(ctx, result)
