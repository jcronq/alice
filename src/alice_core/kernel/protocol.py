"""Kernel + BlockHandler Protocols.

The :class:`Kernel` Protocol is the contract every backend impl must
satisfy. Agent code (turn_runner, kernel_adapter, wake) depends ONLY
on this interface — never on a concrete impl. Use
:func:`alice_core.kernel.factory.make_kernel` to construct the right
impl based on config.

The :class:`BlockHandler` Protocol takes only normalized types
(:class:`TurnSummary`, :class:`SystemEvent`) — no SDK leakage.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .types import KernelResult, KernelSpec, SystemEvent, TurnSummary


__all__ = ["BlockHandler", "Kernel", "NullHandler"]


@runtime_checkable
class BlockHandler(Protocol):
    """Observe blocks and turn outcomes as a kernel runs.

    All methods are async so handlers can do I/O without blocking
    the event loop. Default impls live on :class:`NullHandler` —
    subclass and override only what matters.

    Backend-agnostic: handlers receive normalized types (str text,
    dict tool input, :class:`TurnSummary`, :class:`SystemEvent`).
    """

    async def on_text(self, text: str) -> None: ...
    async def on_tool_use(self, name: str, input: Any, id: str) -> None: ...
    async def on_thinking(self, text: str) -> None: ...
    async def on_user_message(self, content: Any) -> None: ...
    async def on_result(self, summary: TurnSummary) -> None: ...
    async def on_system(self, event: SystemEvent) -> None: ...


class NullHandler:
    """No-op base class — subclass and override what you need."""

    async def on_text(self, text: str) -> None: ...
    async def on_tool_use(self, name: str, input: Any, id: str) -> None: ...
    async def on_thinking(self, text: str) -> None: ...
    async def on_user_message(self, content: Any) -> None: ...
    async def on_result(self, summary: TurnSummary) -> None: ...
    async def on_system(self, event: SystemEvent) -> None: ...


@runtime_checkable
class Kernel(Protocol):
    """Backend-agnostic kernel contract.

    Impls live in :mod:`alice_core.kernel.anthropic` and
    :mod:`alice_pi.kernel`. Agent code never instantiates these
    directly — call :func:`alice_core.kernel.factory.make_kernel`.
    """

    async def run(
        self,
        prompt: str,
        spec: KernelSpec,
        handlers: Optional[list[BlockHandler]] = None,
    ) -> KernelResult: ...
