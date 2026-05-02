"""Kernel factory — single switch point for backend selection.

Agent code (turn_runner, kernel_adapter, wake) calls
:func:`make_kernel` with a :class:`BackendSpec` and gets back a
:class:`Kernel` Protocol instance. The match statement that picks
the impl lives here and ONLY here. Adding a backend means a new
branch in :func:`make_kernel` and nothing else in agent code.

PiKernel (and any future sibling-package kernel) is loaded via
:func:`importlib.import_module` rather than a static ``from ...``
import. Two reasons:

1. **Dependency direction.** :mod:`alice_core` must not statically
   import sibling packages — that's enforced by
   ``tests/test_alice_core_isolation.py``. The dynamic-import
   pattern is the idiomatic plugin-loader shape.
2. **Optional deps.** A deployment that doesn't use pi shouldn't
   need :mod:`alice_pi` installed. Static imports would crash at
   ``alice_core`` import time; dynamic imports surface the missing
   package only when the operator actually selects ``backend: pi``.
"""

from __future__ import annotations

import importlib
from typing import Optional

from ..events import EventEmitter
from .protocol import Kernel


__all__ = ["make_kernel"]


# Backend name -> "module:attribute" path for sibling-package
# kernel impls. Lookup is dynamic so alice_core stays free of
# static sibling-package imports.
_SIBLING_KERNELS: dict[str, str] = {
    "pi": "alice_pi.kernel:PiKernel",
}


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
      :class:`alice_pi.kernel.PiKernel` via
      :func:`importlib.import_module`.
    - ``"subscription"``, ``"api"``, ``"bedrock"`` →
      :class:`AnthropicKernel` (claude_agent_sdk under the hood).
    - Anything else falls through to AnthropicKernel; bad config
      surfaces later via the auth layer rather than at construct
      time.
    """
    harness = getattr(backend, "harness", "")
    name = "pi" if harness == "pi-mono" else getattr(backend, "backend", "subscription")
    sibling = _SIBLING_KERNELS.get(name)
    if sibling is not None:
        module_path, attr = sibling.split(":", 1)
        kernel_cls = getattr(importlib.import_module(module_path), attr)
        return kernel_cls(
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
