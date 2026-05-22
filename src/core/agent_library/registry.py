"""Agent registry — name → :class:`AgentSpec` lookup.

Phase 1 keeps the registry as a Python in-memory dict. Phase 2 will
swap in a file-based loader that hydrates the same shape from
``mind/agents/*.yaml`` — :meth:`Registry.get` is the only surface
callers should depend on.

The module exposes a single :data:`default_registry` instance that
:mod:`core.agent_library.agents` populates at import time. Tests
can build a private :class:`Registry` if they need isolation; the
production code reaches for the default.
"""

from __future__ import annotations

from .types import AgentSpec


__all__ = ["Registry", "default_registry"]


class Registry:
    """A small, in-process name → :class:`AgentSpec` store.

    Not thread-safe — registration happens at import time on a
    single thread. If concurrent registration becomes a thing the
    locking is one ``threading.Lock`` on top of the dict; the
    current code never hits that path so we don't pay the
    overhead.
    """

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        """Register ``spec`` under :attr:`AgentSpec.name`.

        Raises :class:`ValueError` on duplicate names so a typo in
        :mod:`core.agent_library.agents` surfaces at import time
        rather than as a silent overwrite.
        """
        if spec.name in self._specs:
            raise ValueError(
                f"agent {spec.name!r} is already registered; "
                f"call Registry.replace if overwrite is intentional"
            )
        self._specs[spec.name] = spec

    def replace(self, spec: AgentSpec) -> None:
        """Overwrite an existing registration. Tests use this to
        substitute a spec for a single test case without rebuilding
        the whole registry."""
        self._specs[spec.name] = spec

    def get(self, name: str) -> AgentSpec:
        """Look up an :class:`AgentSpec` by name.

        Raises :class:`KeyError` for unknown names — the registry
        is an explicit allowlist, not an open namespace.
        """
        if name not in self._specs:
            known = ", ".join(sorted(self._specs)) or "(none)"
            raise KeyError(
                f"no agent registered as {name!r} (known: {known})"
            )
        return self._specs[name]

    def names(self) -> list[str]:
        """Return registered agent names in sorted order."""
        return sorted(self._specs)

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)


# Module-level singleton. Population happens in
# :mod:`core.agent_library.agents` (imported lazily so this module
# stays dependency-light and importable in isolation).
default_registry = Registry()
