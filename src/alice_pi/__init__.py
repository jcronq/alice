"""alice_pi — Kernel impl backed by pi-coding-agent subprocess.

Public API:

- :class:`PiKernel` — implements :class:`alice_core.kernel.Kernel`
  by spawning the ``pi`` Node binary with ``--mode json`` and
  translating its JSONL event stream to backend-agnostic handler
  calls + a :class:`KernelResult`.

Agent code never imports this directly — use
:func:`alice_core.kernel.factory.make_kernel` with
``BackendSpec(backend="pi")``.
"""

from .kernel import PiKernel


__all__ = ["PiKernel"]
