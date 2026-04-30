"""Surface watcher — internal source for thinking-Alice's surfaced thoughts.

A *surface* is a markdown file dropped into ``inner/surface/`` by the
thinking hemisphere when it wants to voice a thought to the speaking
hemisphere. The watcher polls that directory, queues each new file as
a :class:`SurfaceEvent`, and the dispatcher routes it through this
class's :meth:`handle` to the existing
:func:`alice_speaking._dispatch.handle_surface`.

Phase 5 of plan 01 owns the loop and the dispatched-set bookkeeping
end-to-end. State that's truly per-watcher (which surfaces have
already been pushed onto the queue, which directory we're watching)
lives on the instance. State that's still daemon-shared
(``_archive_unresolved``, the handled directory used by archive)
stays reachable via the daemon-proxy ``ctx`` until Phase 6 extracts
those services.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from dataclasses import dataclass
from typing import Optional

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


# Poll interval for both the surface and emergency watchers. Five
# seconds: short enough that a freshly-written .md surface dispatches
# in human-perceivable time, long enough that an idle daemon doesn't
# burn cycles on `glob("*.md")` every loop.
POLL_SECONDS = 5.0


@dataclass
class SurfaceEvent:
    """A new ``inner/surface/<id>.md`` file ready to dispatch.

    Lives next to :class:`SurfaceWatcher`. Re-exported from
    ``alice_speaking.daemon`` for back-compat with existing test
    imports.
    """

    path: pathlib.Path


class SurfaceWatcher:
    """Internal-source wrapper for surface dispatch.

    Owns the poll loop (:meth:`producer`) and the dispatched-set
    bookkeeping that makes one-surface-one-turn work end-to-end:
    every dispatched filename lands in :attr:`_dispatched`, and
    :meth:`handle` removes it from that set in a ``finally`` block
    so a failed handler doesn't permanently shadow the surface.
    """

    name = "surfaces"
    event_type = SurfaceEvent

    def __init__(self, mind_dir: pathlib.Path) -> None:
        self._surface_dir = mind_dir / "inner" / "surface"
        self._handled_dir = self._surface_dir / ".handled"
        self._dispatched: set[str] = set()

    @property
    def surface_dir(self) -> pathlib.Path:
        """Public access for :func:`_dispatch.handle_surface` and the
        daemon's archive helper, both of which still need the path."""
        return self._surface_dir

    @property
    def handled_dir(self) -> pathlib.Path:
        return self._handled_dir

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Schedule the watch loop. Returns the task so the daemon
        can supervise it under the same start/cancel semantics as a
        transport's producer."""
        return asyncio.create_task(self._run(ctx), name="sur-produce")

    async def _run(self, ctx: DaemonContext) -> None:
        """Poll ``inner/surface/`` for new ``*.md`` files, push each
        as a :class:`SurfaceEvent` onto the dispatcher queue.

        Flat-file only — files in subdirectories of ``inner/surface/``
        (other than ``.handled/``) will not be picked up. A one-shot
        drift check at startup warns when surfaces have been stranded
        in subdirs so the operator notices.
        """
        self._surface_dir.mkdir(parents=True, exist_ok=True)
        self._handled_dir.mkdir(parents=True, exist_ok=True)
        for entry in self._surface_dir.iterdir():
            if entry.is_dir() and entry.name not in (".handled",):
                md_count = len(list(entry.glob("*.md")))
                if md_count:
                    log.warning(
                        "surface drift: %d .md file(s) in subdir %s — "
                        "the watcher is non-recursive; these will not "
                        "dispatch. Move them to flat inner/surface/ "
                        "format.",
                        md_count,
                        entry.name,
                    )
        while not ctx._stop.is_set():
            try:
                for path in sorted(self._surface_dir.glob("*.md")):
                    if path.name.startswith(".") or path.name in self._dispatched:
                        continue
                    self._dispatched.add(path.name)
                    log.info("surface detected: %s", path.name)
                    await ctx._queue.put(SurfaceEvent(path=path))
            except OSError as exc:
                log.warning("surface poll error: %s", exc)
            await asyncio.sleep(POLL_SECONDS)

    async def handle(self, ctx: DaemonContext, event: SurfaceEvent) -> None:
        """Run one surface turn, then release the dispatched-set
        slot so a re-drop of the same filename can dispatch again."""
        from .._dispatch import handle_surface

        try:
            await handle_surface(ctx, event)
        finally:
            self._dispatched.discard(event.path.name)
