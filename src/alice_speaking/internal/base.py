"""Protocol shared by all internal-source modules.

Internal sources are the inbound-only sibling of :class:`Transport`:
they push events onto the daemon queue (``producer``) and run a turn
when the dispatcher hands one back (``handle``), but unlike a transport
they have no outbound concept and no rendering capabilities. Today's
surface watcher (``daemon._surface_producer``) and emergency watcher
(``daemon._emergency_producer``) are the canonical examples; both
move into this package in Phase 5 of plan 01.

The protocol intentionally mirrors :class:`Transport`'s dispatcher
half (``name`` / ``event_type`` / ``producer`` / ``handle``) so the
registry that lands in Phase 3 can store both kinds in the same
event-type → source map without special cases. We keep them as
separate Protocols (rather than collapsing into one ``Source``) so
the ``Transport`` half can grow caps / send / typing without those
leaking onto internal sources.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol, runtime_checkable

from ..transports.base import DaemonContext, Event


@runtime_checkable
class InternalSource(Protocol):
    """A non-conversational event source — internal triggers Alice
    reacts to without an outbound channel.

    Lifecycle is identical to a transport's dispatcher half:
    ``producer(ctx)`` schedules a long-running task that pushes events
    of :attr:`event_type` onto ``ctx._queue``; ``handle(ctx, event)``
    runs one turn for one such event. Phase 3 of plan 01 wires both
    through a registry keyed by ``event_type``.
    """

    name: str
    event_type: type

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Schedule a long-running task that pushes events onto
        ``ctx._queue``. Returns the task so the daemon can supervise
        it, or ``None`` if the source has nothing to do (e.g. the
        underlying directory doesn't exist yet)."""
        ...

    async def handle(self, ctx: DaemonContext, event: Event) -> None:
        """Process one event of :attr:`event_type`."""
        ...
