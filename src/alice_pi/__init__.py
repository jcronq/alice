"""alice_pi — Pi backend support modules (translator, transport,
models registry staging, native pi extensions).

The kernel itself moved to :mod:`alice_core.kernel.pi` so the two
backend impls (Anthropic + Pi) live side by side. This package
still owns the support modules — they stay pi-specific and are
imported by the kernel via ``from alice_pi.* import ...``.

Agent code never imports this directly — use
:func:`alice_core.kernel.factory.make_kernel` with
``BackendSpec(backend="pi")``.
"""

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from alice_core.kernel.pi import PiKernel  # noqa: F401


__all__ = ["PiKernel"]


# Backcompat re-export. The kernel impl moved to alice_core.kernel.pi
# in the refactor/pi-kernel-into-alice-core branch. Update imports;
# this shim will be removed in a later cleanup. PEP 562 lazy
# attribute lookup avoids the circular import that would fire if
# this re-export ran at package-init time (alice_core.kernel.pi
# imports support modules from alice_pi.*).
def __getattr__(name: str) -> Any:
    if name == "PiKernel":
        from alice_core.kernel.pi import PiKernel as _PiKernel

        return _PiKernel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
