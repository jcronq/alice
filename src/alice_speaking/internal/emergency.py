"""Emergency watcher — internal source for external-monitor sentinels.

An *emergency* is a markdown file dropped into ``inner/emergency/`` by
an external monitor (a health-check script, an alert pipeline, the
user's own watchdog) when something needs Alice's attention right
now. Emergencies bypass quiet hours and route to the address book's
emergency recipient via :func:`_dispatch.handle_emergency`.

Phase 3 of plan 01 puts the handler-shaped wrapper in place so the
registry has a uniform ``(event_type → source)`` mapping. Phase 5
moves the producer body (today's ``SpeakingDaemon._emergency_producer``)
into :meth:`producer` so the daemon owns nothing emergency-specific.
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass
from typing import Optional

from ..transports.base import DaemonContext


@dataclass
class EmergencyEvent:
    """A new ``inner/emergency/<id>.md`` file ready to dispatch.

    Lives next to :class:`EmergencyWatcher` (Plan 01 Phase 3 /
    Phase 5). Re-exported from ``alice_speaking.daemon`` for
    back-compat with the ``Event`` union type and existing test
    imports.
    """

    path: pathlib.Path


class EmergencyWatcher:
    """Internal-source wrapper for emergency dispatch.

    Phase 3 only fills :attr:`event_type` and :meth:`handle`.
    :meth:`producer` returns ``None`` because the watcher loop still
    lives on :class:`SpeakingDaemon` (``_emergency_producer``);
    Phase 5 moves it here.
    """

    name = "emergency"
    event_type = EmergencyEvent

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Phase 5 wires this to the sentinel-file watch loop
        currently on :class:`SpeakingDaemon`. Until then the daemon
        schedules ``_emergency_producer`` itself; returning ``None``
        here tells the registry there's nothing to start."""
        return None

    async def handle(self, ctx: DaemonContext, event: EmergencyEvent) -> None:
        """Run one emergency turn. Delegates to the Phase-1 module
        function so the dispatch shape lives in :mod:`_dispatch`."""
        from .._dispatch import handle_emergency

        await handle_emergency(ctx, event)
