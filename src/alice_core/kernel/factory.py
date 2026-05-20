"""Kernel factory — single switch point for backend selection.

Agent code (turn_runner, kernel_adapter, wake) calls
:func:`make_kernel` with a :class:`BackendSpec` and gets back a
:class:`Kernel` Protocol instance. The match statement that picks
the impl lives here and ONLY here. Adding a backend means a new
branch in :func:`make_kernel` and nothing else in agent code.

PiKernel now lives in :mod:`alice_core.kernel.pi` (a subpackage of
the kernel layer), so the factory imports it statically — no more
sibling-package dynamic lookup. The dependency-direction CI guard
(``tests/test_alice_core_isolation.py``) is happy because pi is
inside alice_core.
"""

from __future__ import annotations

from typing import Optional

from ..events import EventEmitter
from .protocol import Kernel


__all__ = ["make_kernel"]


def make_kernel(
    backend: "object",
    emitter: EventEmitter,
    *,
    correlation_id: Optional[str] = None,
    silent: bool = False,
    short_cap: int = 2000,
) -> Kernel:
    """Construct the right :class:`Kernel` impl for ``backend``.

    ``backend`` is :class:`alice_core.config.model.BackendSpec` —
    typed as ``object`` here to avoid a hard import cycle (kernel
    must not depend on config; the contract is the duck-typed
    ``backend.backend`` string attribute).

    Lookup:
    - ``harness="pi-mono"`` / ``backend="pi"`` →
      :class:`alice_core.kernel.pi.PiKernel`.
    - ``"subscription"``, ``"api"``, ``"bedrock"`` →
      :class:`AnthropicKernel` (claude_agent_sdk under the hood).
    - Anything else falls through to AnthropicKernel; bad config
      surfaces later via the auth layer rather than at construct
      time.
    """
    harness = getattr(backend, "harness", "")
    name = "pi" if harness == "pi-mono" else getattr(backend, "backend", "subscription")
    if name == "pi":
        from .pi import PiKernel

        return PiKernel(
            emitter,
            correlation_id=correlation_id,
            silent=silent,
            short_cap=short_cap,
        )
    from .anthropic import AnthropicKernel

    return AnthropicKernel(
        emitter,
        correlation_id=correlation_id,
        silent=silent,
        short_cap=short_cap,
    )
