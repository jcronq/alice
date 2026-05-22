"""Agent library — parameterized agent dispatch on top of the kernel layer.

Phase 1 of issue #194. Consolidates the per-agent inline construction
pattern (system prompt + tool allowlist + behavioral rules scattered
across daemon, dispatcher, design_pipeline) into a single
:class:`AgentSpec` primitive with three constraint layers:

* :class:`ToolPolicy` — pre-execution tool filter (the
  ``allowed_tools`` field on :class:`KernelSpec` is currently accepted
  but never validated).
* :class:`BehavioralRule` — prompt-level behavioral constraints
  injected via the system prompt.
* :class:`OutputSchema` — post-execution validation (placeholder in
  Phase 1; wired in Phase 2+).

The library wraps :class:`core.kernel.KernelSpec` rather than
replacing it. Constraint application happens inside
:func:`runner.run_agent` before kernel dispatch — backend impls
remain untouched.

Phase 1 ships:

* Types: :class:`AgentSpec`, :class:`ToolPolicy`,
  :class:`BehavioralRule`, :class:`OutputSchema`, :class:`PolicyViolation`.
* Built-in policies: :data:`policies.read_only`,
  :data:`policies.full_access`, :data:`policies.exec_only`.
* Module-level :class:`Registry` and two registered agents
  (``thinking``, ``speaking``) sufficient to drive a real kernel
  dispatch.
* Async :func:`runner.run_agent` entry point that applies
  constraints, builds the effective :class:`KernelSpec`, and routes
  through :func:`core.kernel.make_kernel`.

Phase 2+ work (sub-agent inheritance, OutputSchema validation, full
SM-dispatcher migration) is out of scope here — see the design notes
in ``alice-mind/cortex-memory/research/2026-05-13-agent-library-*``
and ``2026-05-14-phase1-implementation-spec.md`` for the roadmap.
"""

from . import policies
from .registry import Registry, default_registry
from .runner import run_agent
from .types import (
    AgentSpec,
    BehavioralRule,
    OutputSchema,
    PolicyViolation,
    ToolPolicy,
)


# Import for its side effect: populates :data:`default_registry`
# with the Phase 1 built-in agents (``thinking``, ``speaking``).
# Kept at the bottom so any consumer that wants to inspect the
# registry through this package gets a populated view.
from . import agents  # noqa: F401, E402


__all__ = [
    "AgentSpec",
    "BehavioralRule",
    "OutputSchema",
    "PolicyViolation",
    "Registry",
    "ToolPolicy",
    "agents",
    "default_registry",
    "policies",
    "run_agent",
]
