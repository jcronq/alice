"""kernels.anthropic — Kernel impl backed by ``claude_agent_sdk``.

Public API:

- :class:`AnthropicKernel` — implements :class:`core.kernel.Kernel`
  by driving the Claude Agent SDK's async ``query()`` generator to
  completion, emitting structured events at every block boundary,
  and returning a :class:`KernelResult`.

Agent code never imports this directly — use
:func:`core.kernel.factory.make_kernel` with
``BackendSpec(backend="subscription" | "api" | "bedrock")``.
"""

from .kernel import AnthropicKernel


__all__ = ["AnthropicKernel"]
