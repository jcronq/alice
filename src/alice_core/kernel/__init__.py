"""Kernel layer — backend-agnostic Protocol + impls.

Public API:

- :class:`Kernel` — the Protocol every backend impl satisfies.
- :class:`KernelSpec` — backend-agnostic per-turn config.
- :class:`KernelResult` — backend-agnostic per-turn result.
- :class:`UsageInfo`, :class:`TurnSummary`, :class:`SystemEvent`,
  :data:`ThinkingLevel` — normalized handler-input + result types.
- :class:`BlockHandler` Protocol + :class:`NullHandler` base class.
- :class:`AnthropicKernel` — claude_agent_sdk-backed impl.
- :class:`PiKernel` — pi-coding-agent-backed impl. Lives next to
  :class:`AnthropicKernel` so both backends are visible side by side;
  pi-specific support modules (translator, transport, models
  registry staging, native extensions) still live in
  :mod:`alice_pi`.
- :func:`make_kernel` — single switch point for backend selection
  (lives in :mod:`alice_core.kernel.factory`; re-exported here for
  ergonomics once Phase B lands).

Agent code should import the Protocol + types only — never a
concrete impl. Use :func:`make_kernel` to construct.
"""

from typing import TYPE_CHECKING, Any

from .anthropic import AnthropicKernel
from .factory import make_kernel
from .protocol import BlockHandler, Kernel, NullHandler
from .types import (
    KernelResult,
    KernelSpec,
    SystemEvent,
    ThinkingLevel,
    TurnSummary,
    UsageInfo,
)


if TYPE_CHECKING:
    from .pi import PiKernel  # noqa: F401


__all__ = [
    "AnthropicKernel",
    "BlockHandler",
    "Kernel",
    "KernelResult",
    "KernelSpec",
    "NullHandler",
    "PiKernel",
    "SystemEvent",
    "ThinkingLevel",
    "TurnSummary",
    "UsageInfo",
    "make_kernel",
]


# PEP 562 lazy attribute lookup for PiKernel. Eager-importing it at
# package init time causes a circular import: ``alice_core.kernel.pi``
# imports support modules from ``alice_pi.*`` (translator, transport,
# models_staging), and ``alice_pi.translator`` re-imports symbols from
# ``alice_core.kernel`` — which is still mid-init at that point. The
# lazy lookup defers ``.pi`` import until the first attribute access,
# by which time ``alice_core.kernel`` is fully populated.
def __getattr__(name: str) -> Any:
    if name == "PiKernel":
        from .pi import PiKernel as _PiKernel

        return _PiKernel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
