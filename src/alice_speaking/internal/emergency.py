"""Emergency watcher — internal source for external-monitor sentinels.

An *emergency* is a markdown file dropped into ``inner/emergency/`` by
an external monitor (a health-check script, an alert pipeline, the
user's own watchdog) when something needs Alice's attention right
now. Emergencies bypass quiet hours and route to the address book's
emergency recipient via :func:`_dispatch.handle_emergency`.

Phase 5 of plan 01 owns the loop and the dispatched-set bookkeeping
end-to-end. Same shape as :class:`SurfaceWatcher`; the only real
difference is the directory and the urgency.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from dataclasses import dataclass
from typing import Optional

from ..transports.base import DaemonContext
from .surfaces import POLL_SECONDS


log = logging.getLogger(__name__)


@dataclass
class EmergencyEvent:
    """A new ``inner/emergency/<id>.md`` file ready to dispatch.

    Lives next to :class:`EmergencyWatcher`. Re-exported from
    ``alice_speaking.daemon`` for back-compat with existing test
    imports.
    """

    path: pathlib.Path


class EmergencyWatcher:
    """Internal-source wrapper for emergency dispatch.

    Owns the poll loop (:meth:`producer`) and the dispatched-set
    bookkeeping. Identical shape to :class:`SurfaceWatcher`; kept
    as a separate class because the directory and urgency are
    different and the two will diverge as Alice's emergency handling
    grows (rate-limiting, escalation policies, etc.).
    """

    name = "emergency"
    event_type = EmergencyEvent

    def __init__(self, mind_dir: pathlib.Path) -> None:
        self._emergency_dir = mind_dir / "inner" / "emergency"
        self._handled_dir = self._emergency_dir / ".handled"
        self._dispatched: set[str] = set()

    @property
    def emergency_dir(self) -> pathlib.Path:
        return self._emergency_dir

    @property
    def handled_dir(self) -> pathlib.Path:
        return self._handled_dir

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        return asyncio.create_task(self._run(ctx), name="emg-produce")

    async def _run(self, ctx: DaemonContext) -> None:
        """Poll ``inner/emergency/`` for new ``*.md`` sentinels, push
        each as an :class:`EmergencyEvent` onto the dispatcher queue."""
        self._emergency_dir.mkdir(parents=True, exist_ok=True)
        self._handled_dir.mkdir(parents=True, exist_ok=True)
        while not ctx._stop.is_set():
            try:
                for path in sorted(self._emergency_dir.glob("*.md")):
                    if path.name.startswith(".") or path.name in self._dispatched:
                        continue
                    self._dispatched.add(path.name)
                    log.warning("EMERGENCY detected: %s", path.name)
                    await ctx._queue.put(EmergencyEvent(path=path))
            except OSError as exc:
                log.warning("emergency poll error: %s", exc)
            await asyncio.sleep(POLL_SECONDS)

    async def handle(self, ctx: DaemonContext, event: EmergencyEvent) -> None:
        """Run one emergency turn, then release the dispatched-set
        slot so a re-drop of the same filename can dispatch again."""
        from .._dispatch import handle_emergency

        try:
            await handle_emergency(ctx, event)
        finally:
            self._dispatched.discard(event.path.name)
