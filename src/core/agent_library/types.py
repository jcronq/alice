"""Agent library types — :class:`AgentSpec` and its constraint layers.

The four orthogonal axes from the design synthesis
(``2026-05-13-agent-library-design-synthesis``) live on
:class:`AgentSpec`: persona, runtime, scope, lifecycle. Three
constraint layers (:class:`ToolPolicy`, :class:`BehavioralRule`,
:class:`OutputSchema`) sit alongside the wrapped
:class:`core.kernel.KernelSpec`.

Everything in this module is a dataclass with no behavior beyond the
small helpers needed to translate the spec into an effective
:class:`KernelSpec`. Dispatch (build kernel, run turn) lives in
:mod:`core.agent_library.runner`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Optional

from ..kernel import KernelSpec


__all__ = [
    "AgentSpec",
    "BehavioralRule",
    "OutputSchema",
    "PolicyViolation",
    "ToolPolicy",
]


# Phase 1 supports allow / deny shapes; later phases may add
# "transform" or "callable" types for richer policies.
ToolPolicyType = Literal["allow", "deny"]


class PolicyViolation(RuntimeError):
    """Raised by :meth:`AgentSpec.build_spec` when the active policy
    leaves no tools available. The runner surfaces this before any
    kernel dispatch so the caller can decide whether to fall back
    (drop the policy) or fail loud (refuse to run unconstrained)."""


@dataclass(frozen=True)
class ToolPolicy:
    """Pre-execution tool filter.

    Either an allowlist or a denylist — the policy ``type`` picks
    which field is consulted. :meth:`evaluate` is pure: it takes the
    set of tools the kernel spec requested and returns the subset
    permitted under this policy.

    Frozen so policies can live as module-level singletons in
    :mod:`core.agent_library.policies` without accidental mutation.
    """

    type: ToolPolicyType
    allowlist: frozenset[str] = field(default_factory=frozenset)
    denylist: frozenset[str] = field(default_factory=frozenset)

    def evaluate(self, requested: set[str]) -> set[str]:
        if self.type == "allow":
            return requested & self.allowlist
        return requested - self.denylist


@dataclass(frozen=True)
class BehavioralRule:
    """Prompt-level behavioral constraint.

    Phase 1 only supports ``condition="always"`` — every registered
    rule is injected on every dispatch. Conditional injection (e.g.,
    only inject when the model has access to a network tool) lands
    in Phase 2+.

    The ``id`` field surfaces in the injected text so an operator
    reading the rendered system prompt can see which rules are
    active. ``injection`` is raw text — no templating, no
    placeholders.
    """

    id: str
    injection: str
    condition: Literal["always"] = "always"

    def render(self) -> str:
        """Return the text block this rule contributes to the system
        prompt. Heading uses the rule ``id`` for auditability."""
        return f"## Constraint: {self.id}\n{self.injection}".rstrip()


@dataclass(frozen=True)
class OutputSchema:
    """Placeholder for Phase 2+ output validation.

    Phase 1 stores the schema name only — no validation is performed.
    The field exists on :class:`AgentSpec` so the registry can record
    expected output shapes without forcing every flavor to declare
    them right now. Phase 2 wires actual validation (Pydantic model
    string → compiled validator at registration time).
    """

    name: str = ""


@dataclass(frozen=True)
class AgentSpec:
    """A named, constraint-wrapped configuration for one agent flavor.

    The four orthogonal axes (persona, runtime, scope, lifecycle) sit
    alongside the wrapped :class:`KernelSpec`. The constraint layers
    (tool policy, behavioral rules, output schema) are applied via
    :meth:`build_spec` to produce the effective :class:`KernelSpec`
    that gets dispatched.

    ``runtime`` is an opaque string today — the runner picks the
    actual backend at dispatch time. Phase 2 may turn this into a
    typed :class:`core.config.model.BackendSpec` reference.

    Frozen + ``tuple`` (not ``list``) for the rules field so
    registered specs are immutable. Use :func:`dataclasses.replace`
    if you need a derived spec.
    """

    name: str
    persona: str
    kernel_spec: KernelSpec
    runtime: str = "claude-agent-sdk"
    scope: str = "on-demand"
    lifecycle: str = "per-issue"
    tool_policy: Optional[ToolPolicy] = None
    behavioral_constraints: tuple[BehavioralRule, ...] = ()
    output_schema: Optional[OutputSchema] = None

    def effective_tools(self) -> list[str]:
        """Return the tool set after :attr:`tool_policy` is applied,
        sorted for determinism. Raises :class:`PolicyViolation` if
        the policy drains the set entirely (the caller would dispatch
        a kernel with no tools, which is almost certainly a bug)."""
        requested = set(self.kernel_spec.allowed_tools)
        if self.tool_policy is None:
            return sorted(requested)
        effective = self.tool_policy.evaluate(requested)
        if not effective:
            raise PolicyViolation(
                f"agent {self.name!r}: no tools available under "
                f"policy {self.tool_policy.type!r}"
            )
        return sorted(effective)

    def assembled_system_prompt(self) -> Optional[str]:
        """Merge the kernel spec's base ``append_system_prompt`` with
        every behavioral rule's injection block. Returns ``None`` if
        the result is empty (matches :class:`KernelSpec`'s "unset"
        semantics — passing ``""`` would still hit the backend)."""
        # Imported lazily so :mod:`prompt_assembly` can depend on
        # :class:`BehavioralRule` from this module without a cycle.
        from .prompt_assembly import merge

        return merge(
            base=self.kernel_spec.append_system_prompt,
            rules=self.behavioral_constraints,
        )

    def build_spec(self) -> KernelSpec:
        """Return the effective :class:`KernelSpec` that the runner
        feeds to :func:`core.kernel.make_kernel`. Pure — does not
        mutate :attr:`kernel_spec`."""
        return replace(
            self.kernel_spec,
            allowed_tools=self.effective_tools(),
            append_system_prompt=self.assembled_system_prompt(),
        )
