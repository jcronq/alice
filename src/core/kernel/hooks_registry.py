"""Composition + module-level registry for :class:`AgentHook` instances.

PR1 landed the bare hook surface in :mod:`core.kernel.hooks`
(:class:`AgentHook` Protocol, :class:`BaseAgentHook`,
:class:`TurnResult`, :class:`Reporter`). PR2 — this module — adds:

- :class:`CompositeAgentHook`: a chaining adapter that fans the four
  lifecycle methods out across an ordered list of child hooks.
  Mutator returns chain through (each hook sees the previous hook's
  output); ``after_turn`` injections concatenate with newlines;
  ``after_agent_end`` is sequential and discards return values.
- :func:`register_agent_hook`: append a hook to the module-level
  registry, scoped to one or more roles (``speaking`` / ``thinking``
  / ``worker``) or globally.
- :func:`hooks_for`: build a fresh :class:`CompositeAgentHook` for a
  given role, including any globally-registered hooks.
- :func:`get_all_hooks`: return every registered hook (global +
  role-scoped) — useful for introspection / debugging.

This module is pure plumbing. PR3 wires the composite into the
run-loop; PR4 ships the first concrete consumer (the markdown
frontmatter validator). The registry is module-state by design —
registration happens at import time, single-threaded, before any
kernel runs, so thread safety is not required.
"""

from __future__ import annotations

from typing import Optional

from claude_agent_sdk import Message

from .hooks import AgentHook, Reporter, TurnResult
from .types import KernelResult, KernelSpec


__all__ = [
    "CompositeAgentHook",
    "register_agent_hook",
    "hooks_for",
    "get_all_hooks",
]


# ---------------------------------------------------------------------------
# CompositeAgentHook — fan four lifecycle methods across N child hooks
# ---------------------------------------------------------------------------


class CompositeAgentHook:
    """Chains N :class:`AgentHook` instances.

    Each lifecycle method iterates through ``hooks`` in registration
    order. Semantics per method:

    - :meth:`before_agent_start` / :meth:`before_turn`: mutator chain.
      Each hook sees the output of the previous hook; ``None`` means
      "no change" and the previous value flows through.
    - :meth:`after_turn`: each child sees the *same* ``turn_result``
      (no chaining on input). Non-empty returns are joined with
      newlines into a single injection string; if no hook injects,
      ``None`` is returned.
    - :meth:`after_agent_end`: sequential invocation, return values
      discarded — it's an observation-only hook.

    The composite itself satisfies the :class:`AgentHook` Protocol,
    so it can be nested inside another composite if a future caller
    wants to layer scopes.

    ``reporter`` is stored at the composite level so a future variant
    can inject one into children that don't carry their own. PR2
    doesn't forward it automatically — children built via
    :class:`BaseAgentHook` already receive their reporter at
    construction time.
    """

    def __init__(
        self,
        hooks: list[AgentHook],
        reporter: Optional[Reporter] = None,
    ) -> None:
        self._hooks = list(hooks)
        self._reporter = reporter

    async def before_agent_start(
        self, spec: KernelSpec
    ) -> Optional[KernelSpec]:
        result = spec
        for hook in self._hooks:
            r = await hook.before_agent_start(result)
            if r is not None:
                result = r
        return result

    async def before_turn(
        self, messages: list[Message]
    ) -> Optional[list[Message]]:
        result = messages
        for hook in self._hooks:
            r = await hook.before_turn(result)
            if r is not None:
                result = r
        return result

    async def after_turn(self, turn_result: TurnResult) -> Optional[str]:
        injections: list[str] = []
        for hook in self._hooks:
            inj = await hook.after_turn(turn_result)
            if inj:
                injections.append(inj)
        return "\n".join(injections) if injections else None

    async def after_agent_end(self, result: KernelResult) -> None:
        for hook in self._hooks:
            await hook.after_agent_end(result)


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------


# Role-scoped hooks. Keys are the canonical role names. ``thinking``
# is included for symmetry — PR3/PR4 only register on ``speaking``
# and ``worker``, but the slot exists so a future hook can scope
# there without touching this module.
_REGISTRY: dict[str, list[AgentHook]] = {
    "speaking": [],
    "thinking": [],
    "worker": [],
}

# Global hooks — applied to every role's composite. Use sparingly:
# most hooks should scope to a role.
_ALL_HOOKS: list[AgentHook] = []


def register_agent_hook(
    hook: AgentHook,
    *,
    roles: Optional[list[str]] = None,
) -> None:
    """Register ``hook`` for one or more roles.

    Args:
        hook: The :class:`AgentHook` implementation to register.
        roles: Role names to scope this hook to. ``None`` (or the
            sentinel ``["*"]``) registers the hook globally — it
            will be included in every role's composite via
            :func:`hooks_for`. Otherwise the hook is appended to the
            registry list for each named role; unknown role names
            are accepted (the registry grows to accommodate them).

    The registry is append-only and order-preserving: a hook
    registered first runs first inside the composite.
    """
    effective = roles if roles is not None else ["*"]
    if effective == ["*"]:
        _ALL_HOOKS.append(hook)
        return
    for role in effective:
        if role not in _REGISTRY:
            _REGISTRY[role] = []
        _REGISTRY[role].append(hook)


def hooks_for(role: str) -> CompositeAgentHook:
    """Return a fresh :class:`CompositeAgentHook` for ``role``.

    Composition order: role-scoped hooks first (in registration
    order), then global hooks. Each call constructs a new composite
    — there is no caching, since registration is import-time and the
    cost of building the composite is negligible compared to a model
    turn. The returned composite is independent: mutating its
    ``_hooks`` list does not affect the registry.

    If ``role`` is unknown, the composite contains only the global
    hooks.
    """
    hooks: list[AgentHook] = list(_REGISTRY.get(role, []))
    hooks.extend(_ALL_HOOKS)
    return CompositeAgentHook(hooks)


def get_all_hooks() -> list[AgentHook]:
    """Return every registered hook (global + every role-scoped).

    Order: globals first, then role-scoped in the iteration order of
    :data:`_REGISTRY` (insertion-order: ``speaking``, ``thinking``,
    ``worker``, plus any extras added at runtime). Intended for
    introspection and debugging — the runtime uses
    :func:`hooks_for` instead.
    """
    return list(_ALL_HOOKS) + [
        hook for hooks in _REGISTRY.values() for hook in hooks
    ]
