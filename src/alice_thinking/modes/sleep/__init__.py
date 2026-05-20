"""Sleep-mode wrapper — delegates to :class:`PhaseRunner`.

The sleep window (23:00–06:59 local) historically dispatched to
:class:`ConsolidationStage`. Phase routing (design:
``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``)
folds that behavior into :class:`PhaseRunner`. This module exposes
``SleepMode`` as a thin Mode wrapper for backward compatibility —
new code should call ``PhaseRunner.run(Phase.SLEEP_*, ctx)`` directly.

Phase 0 of the migration always picks :attr:`Phase.SLEEP_B`
(matching today's behavior). Phase 3 unlocks B/C/D dispatch by
flipping ``PhaseConfig.enable_full_sleep_dispatch=True`` and reading
the chosen phase from the snapshot — but that's caller territory;
``SleepMode`` itself stays B-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..base import WakeContext, _NullPostRun
from ...phase import Phase, PhaseConfig
from ...runtime import PhaseRunner


if TYPE_CHECKING:
    from core.kernel import KernelResult, KernelSpec


__all__ = ["SleepMode"]


class SleepMode(_NullPostRun):
    """Stub-equivalent SleepMode wrapper.

    Always uses :attr:`Phase.SLEEP_B`. Phase 3 of the migration moves
    the sub-stage choice into :func:`select_phase`; callers that need
    B/C/D dispatch should consume the phase directly via
    :class:`PhaseRunner`.

    ``mcp_servers`` mirrors :class:`ActiveMode` — wake.py builds the
    thinking-side MCP server once and threads the dict through both
    mode constructors so ``run_experiment`` is reachable from any
    real phase.
    """

    name = "sleep"

    def __init__(
        self,
        runner: Optional[PhaseRunner] = None,
        config: Optional[PhaseConfig] = None,
        phase: Phase = Phase.SLEEP_B,
        *,
        mcp_servers: Optional[dict[str, Any]] = None,
    ) -> None:
        self._runner = runner or PhaseRunner(config=config)
        self._phase = phase
        self._mcp_servers = mcp_servers

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def stage(self) -> str:
        """Back-compat: telemetry consumers expect a stage label like
        ``"sleep:consolidate"``. Phase value strings carry richer
        information (``"sleep_b"`` etc.) — return the phase value
        directly so the wake_start event surfaces the new naming.
        """
        return self._phase.value

    def kernel_spec(self, ctx: WakeContext) -> "KernelSpec":
        return self._runner.kernel_spec(
            self._phase, ctx, mcp_servers=self._mcp_servers
        )

    async def build_prompt(self, ctx: WakeContext) -> str:
        return self._runner.build_prompt(self._phase, ctx)

    async def post_run(self, ctx: WakeContext, result: "KernelResult") -> None:
        return None
