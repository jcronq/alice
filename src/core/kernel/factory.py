"""Kernel factory — single switch point for backend selection.

Agent code (turn_runner, kernel_adapter, wake) calls
:func:`make_kernel` with a :class:`BackendSpec` and gets back a
:class:`Kernel` Protocol instance. The match statement that picks
the impl lives here and ONLY here. Adding a backend means a new
entry in :data:`_SIBLING_KERNELS` and nothing else in agent code.

Every backend (and any future sibling-package kernel) is loaded
via :func:`importlib.import_module` rather than a static
``from ...`` import. Two reasons:

1. **Dependency direction.** :mod:`core` must not statically
   import sibling packages — that's enforced by
   ``tests/test_core_isolation.py``. The dynamic-import pattern is
   the idiomatic plugin-loader shape.
2. **Optional deps.** A deployment that doesn't use a backend
   shouldn't need the backend's package installed. Static imports
   would crash at :mod:`core` import time; dynamic imports
   surface the missing package only when the operator actually
   selects that backend.
"""

from __future__ import annotations

import importlib
from typing import Optional

from ..events import EventEmitter
from .protocol import Kernel


__all__ = ["make_kernel"]


# Backend name -> "module:attribute" path for sibling-package
# kernel impls. Lookup is dynamic so core stays free of
# static sibling-package imports.
_SIBLING_KERNELS: dict[str, str] = {
    "anthropic": "kernels.anthropic.kernel:AnthropicKernel",
    "pi": "kernels.pi.kernel:PiKernel",
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

    ``backend`` is :class:`core.config.model.BackendSpec` —
    typed as ``object`` here to avoid a hard import cycle (kernel
    must not depend on config; the contract is the duck-typed
    ``backend.backend`` string attribute).

    Lookup:
    - ``harness="pi-mono"`` / ``backend="pi"`` →
      :class:`kernels.pi.kernel.PiKernel`.
    - ``"subscription"``, ``"api"``, ``"bedrock"`` →
      :class:`kernels.anthropic.kernel.AnthropicKernel`
      (claude_agent_sdk under the hood).
    - Anything else falls through to AnthropicKernel; bad config
      surfaces later via the auth layer rather than at construct
      time.
    """
    harness = getattr(backend, "harness", "")
    name = "pi" if harness == "pi-mono" else getattr(backend, "backend", "subscription")
    # Anthropic-SDK backends share one impl.
    if name in {"subscription", "api", "bedrock"}:
        name = "anthropic"
    sibling = _SIBLING_KERNELS.get(name, _SIBLING_KERNELS["anthropic"])
    module_path, attr = sibling.split(":", 1)
    kernel_cls = getattr(importlib.import_module(module_path), attr)
    return kernel_cls(
        emitter,
        correlation_id=correlation_id,
        silent=silent,
        short_cap=short_cap,
    )
