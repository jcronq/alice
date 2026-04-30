"""Mode protocol + WakeContext — Plan 03 Phase 2.

A :class:`Mode` is one wake-time strategy: build the prompt for this
particular kind of wake, hand the kernel a spec, optionally do
post-run cleanup. Today (Phase 2) only :class:`ActiveMode` exists;
Phase 3 introduces ``SleepMode`` with sub-stage dispatch.

The shape mirrors plan 01's ``InternalSource`` / ``Transport`` —
small protocols, explicit dependencies, easy to test.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Protocol


if TYPE_CHECKING:
    from alice_core.config.personae import Personae
    from alice_core.kernel import KernelResult, KernelSpec


@dataclass(frozen=True)
class WakeContext:
    """Per-wake fixed state — same role as plan 01's ``DaemonContext``.

    Built once at the top of ``main()`` and passed to the selected
    mode. Modes read the fields they need; new modes don't need new
    function signatures.
    """

    mind_dir: pathlib.Path
    cwd: pathlib.Path
    now: datetime
    personae: "Personae"
    model: str
    max_seconds: int
    tools: list[str]
    system_prompt: str
    quick: bool = False
    inline_prompt: Optional[str] = None
    bootstrap_path: Optional[pathlib.Path] = None
    directive_path: Optional[pathlib.Path] = None


class Mode(Protocol):
    """One wake-time strategy.

    ``name`` ends up in the wake's emitted events as ``mode=...`` so
    the viewer + telemetry can see which mode ran without parsing
    the prompt body.
    """

    name: str

    def kernel_spec(self, ctx: WakeContext) -> "KernelSpec": ...

    async def build_prompt(self, ctx: WakeContext) -> str: ...

    async def post_run(self, ctx: WakeContext, result: "KernelResult") -> None: ...


class _NullPostRun:
    """Mixin that supplies a no-op ``post_run``. Most modes don't
    need cleanup; subclassing this keeps them concise."""

    async def post_run(
        self, ctx: WakeContext, result: "KernelResult"
    ) -> None:  # pragma: no cover - trivial
        return None
