"""Async runner for :class:`AgentSpec`.

The runner is the thin layer between an :class:`AgentSpec` and the
backend kernel. It applies the spec's constraint layers (tool
policy + behavioral rules → effective :class:`KernelSpec`), picks a
:class:`Kernel` impl via :func:`core.kernel.make_kernel`, and
dispatches one turn.

Backend selection: the caller passes a ``backend`` object
(:class:`core.config.model.BackendSpec` in practice) that the kernel
factory uses to pick AnthropicKernel vs PiKernel. The runner stays
duck-typed on ``backend`` to match :func:`make_kernel`'s shape —
this avoids a hard import cycle and keeps the runner trivially
testable with a stub backend.

OutputSchema validation is deliberately a no-op in Phase 1. The
:class:`OutputSchema` field exists on :class:`AgentSpec` so registry
entries can record expected output shapes, but the validator wires
in during Phase 2+.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..kernel import KernelResult, make_kernel
from .types import AgentSpec


if TYPE_CHECKING:
    from ..events import EventEmitter


__all__ = ["run_agent"]


async def run_agent(
    agent: AgentSpec,
    *,
    prompt: str,
    emitter: "EventEmitter",
    backend: object = None,
    correlation_id: Optional[str] = None,
) -> KernelResult:
    """Dispatch one turn through ``agent``'s constraint-applied kernel
    spec.

    Steps:

    1. :meth:`AgentSpec.build_spec` applies the tool policy and
       merges behavioral rules into ``append_system_prompt``,
       producing the effective :class:`KernelSpec`.
    2. :func:`core.kernel.make_kernel` picks the backend impl from
       ``backend`` (defaults to subscription Anthropic so legacy
       callers without an explicit backend keep working).
    3. ``await kernel.run(prompt, spec)`` runs the turn and returns
       the :class:`KernelResult`.

    Raises :class:`core.agent_library.types.PolicyViolation` (from
    :meth:`AgentSpec.effective_tools` via :meth:`build_spec`) when
    the tool policy leaves no tools available. The runner does not
    catch it — the caller decides whether that's a fatal error or a
    fallback signal.
    """
    spec = agent.build_spec()

    if backend is None:
        # Defer the import — :mod:`core.config.model` is part of
        # ``core`` so the AST isolation guard is happy, but the
        # lazy import keeps :mod:`core.agent_library.runner`
        # importable even when no backend config exists yet
        # (e.g., very early-boot tests).
        from ..config.model import BackendSpec

        backend = BackendSpec(backend="subscription")

    kernel = make_kernel(backend, emitter, correlation_id=correlation_id)
    return await kernel.run(prompt, spec)
