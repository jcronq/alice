"""Surface watcher — internal source for thinking-Alice's surfaced thoughts.

A *surface* is a markdown file dropped into ``inner/surface/`` by the
thinking hemisphere when it wants to voice a thought to the speaking
hemisphere. The watcher polls that directory, queues each new file as
a :class:`SurfaceEvent`, and the dispatcher routes it through this
class's :meth:`handle` to the existing
:func:`alice_speaking._dispatch.handle_surface`.

Phase 3 of plan 01 puts the handler-shaped wrapper in place so the
registry has a uniform ``(event_type → source)`` mapping. Phase 5
moves the producer body (today's ``SpeakingDaemon._surface_producer``)
into :meth:`producer` so the daemon owns nothing surface-specific.
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass
from typing import Optional

from ..transports.base import DaemonContext


@dataclass
class SurfaceEvent:
    """A new ``inner/surface/<id>.md`` file ready to dispatch.

    Lives next to :class:`SurfaceWatcher` (Plan 01 Phase 3 / Phase 5).
    Re-exported from ``alice_speaking.daemon`` for back-compat with
    the ``Event`` union type and existing test imports.
    """

    path: pathlib.Path


class SurfaceWatcher:
    """Internal-source wrapper for surface dispatch.

    Phase 3 only fills :attr:`event_type` and :meth:`handle`.
    :meth:`producer` returns ``None`` because the watcher loop still
    lives on :class:`SpeakingDaemon` (``_surface_producer``); Phase 5
    moves it here.
    """

    name = "surfaces"
    event_type = SurfaceEvent

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Phase 5 wires this to the inotify/poll loop currently on
        :class:`SpeakingDaemon`. Until then the daemon schedules
        ``_surface_producer`` itself; returning ``None`` here tells
        the registry there's nothing to start."""
        return None

    async def handle(self, ctx: DaemonContext, event: SurfaceEvent) -> None:
        """Run one surface turn. Delegates to the Phase-1 module
        function so the dispatch shape lives in :mod:`_dispatch`."""
        from .._dispatch import handle_surface

        await handle_surface(ctx, event)
