"""Protocol for once-per-session startup tasks.

Distinct from :class:`Transport` and :class:`InternalSource`: those
two run continuously and push events onto the dispatcher queue;
``StartupSource`` runs exactly once at session start, before the
dispatcher's event loop accepts its first event, and modifies
``ctx`` directly (priming state, queueing deferred events) instead
of producing its own event_type.

The clean separation matters because forcing a one-shot task into
the producer/handler shape would require fake events and special-case
task-completion handling — see "Why a separate StartupSource and
not 'internal source that fires once'" in
``docs/refactor/01-transport-plugin-interface.md``.

Phase 2 of plan 01 ships the protocol; Phase 5 lands the first
concrete implementations alongside the internal sources.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..transports.base import DaemonContext


@runtime_checkable
class StartupSource(Protocol):
    """A task that runs exactly once before the dispatcher loop starts.

    Failures are logged; they don't block the daemon from starting
    (each source is best-effort). Sources that need to cancel
    daemon startup should raise — the dispatcher's ``_run_startup``
    decides per-source whether to swallow or propagate.
    """

    name: str

    async def run_once(self, ctx: DaemonContext) -> None:
        """Execute the startup task once. Must complete promptly —
        the dispatcher waits for every startup source to finish
        before accepting the first event."""
        ...
