"""ActiveMode — thin wrapper that delegates to :class:`PhaseRunner`.

Plan 03 Phase 2 introduced the :class:`Mode` protocol; the phase-routing
work (design: ``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``)
collapses prompt assembly + kernel spec construction into
:class:`alice_thinking.runtime.PhaseRunner`. ``ActiveMode`` now exists
to satisfy callers that still want a Mode object — every method
forwards to :class:`PhaseRunner` with :attr:`Phase.ACTIVE`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from alice_core.kernel import KernelSpec

from ..phase import Phase, PhaseConfig
from ..runtime import PhaseRunner
from .base import WakeContext, _NullPostRun


if TYPE_CHECKING:
    pass


class ActiveMode(_NullPostRun):
    """The 07:00–22:59 mode.

    Delegates to :class:`PhaseRunner` — both methods compose the
    same prompt + spec the runner produces for :attr:`Phase.ACTIVE`.

    ``mcp_servers`` (optional) is threaded onto the KernelSpec for
    every wake driven by this mode. wake.py builds the thinking-side
    MCP server (``mcp__alice__run_experiment`` et al.) once per wake
    and hands the dict in here.
    """

    name = "active"

    def __init__(
        self,
        runner: Optional[PhaseRunner] = None,
        config: Optional[PhaseConfig] = None,
        *,
        mcp_servers: Optional[dict[str, Any]] = None,
    ) -> None:
        self._runner = runner or PhaseRunner(config=config)
        self._mcp_servers = mcp_servers

    def kernel_spec(self, ctx: WakeContext) -> KernelSpec:
        return self._runner.kernel_spec(
            Phase.ACTIVE, ctx, mcp_servers=self._mcp_servers
        )

    async def build_prompt(self, ctx: WakeContext) -> str:
        return self._runner.build_prompt(Phase.ACTIVE, ctx)
