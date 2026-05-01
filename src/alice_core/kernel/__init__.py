"""Kernel layer — backend-agnostic Protocol + impls.

Public API:

- :class:`Kernel` — the Protocol every backend impl satisfies.
- :class:`KernelSpec` — backend-agnostic per-turn config.
- :class:`KernelResult` — backend-agnostic per-turn result.
- :class:`UsageInfo`, :class:`TurnSummary`, :class:`SystemEvent`,
  :data:`ThinkingLevel` — normalized handler-input + result types.
- :class:`BlockHandler` Protocol + :class:`NullHandler` base class.
- :class:`AnthropicKernel` — first impl (claude_agent_sdk-backed).
- :func:`make_kernel` — single switch point for backend selection
  (lives in :mod:`alice_core.kernel.factory`; re-exported here for
  ergonomics once Phase B lands).

Agent code should import the Protocol + types only — never a
concrete impl. Use :func:`make_kernel` to construct.
"""

from .anthropic import AnthropicKernel
from .protocol import BlockHandler, Kernel, NullHandler
from .types import (
    KernelResult,
    KernelSpec,
    SystemEvent,
    ThinkingLevel,
    TurnSummary,
    UsageInfo,
)


__all__ = [
    "AnthropicKernel",
    "BlockHandler",
    "Kernel",
    "KernelResult",
    "KernelSpec",
    "NullHandler",
    "SystemEvent",
    "ThinkingLevel",
    "TurnSummary",
    "UsageInfo",
]
