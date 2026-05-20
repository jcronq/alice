"""Kernel layer — backend-agnostic Protocol + types + factory.

Public API:

- :class:`Kernel` — the Protocol every backend impl satisfies.
- :class:`KernelSpec` — backend-agnostic per-turn config.
- :class:`KernelResult` — backend-agnostic per-turn result.
- :class:`UsageInfo`, :class:`TurnSummary`, :class:`SystemEvent`,
  :data:`ThinkingLevel` — normalized handler-input + result types.
- :class:`BlockHandler` Protocol + :class:`NullHandler` base class.
- :func:`make_kernel` — single switch point for backend selection
  (lives in :mod:`core.kernel.factory`; re-exported here for
  ergonomics).

Agent code should import the Protocol + types only — never a
concrete impl. Use :func:`make_kernel` to construct. Concrete
backends live in sibling packages: :mod:`kernels.anthropic`,
:mod:`kernels.pi`. They are loaded dynamically through
:func:`make_kernel` and must not be imported statically here —
``tests/test_core_isolation.py`` enforces the boundary.
"""

from .factory import make_kernel
from .hooks import (
    AgentHook,
    BaseAgentHook,
    LoggingReporter,
    Reporter,
    ToolResult,
    ToolUse,
    TurnResult,
)
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
    "AgentHook",
    "BaseAgentHook",
    "BlockHandler",
    "Kernel",
    "KernelResult",
    "KernelSpec",
    "LoggingReporter",
    "NullHandler",
    "Reporter",
    "SystemEvent",
    "ThinkingLevel",
    "ToolResult",
    "ToolUse",
    "TurnResult",
    "TurnSummary",
    "UsageInfo",
    "make_kernel",
]
