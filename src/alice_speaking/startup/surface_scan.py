"""Startup scan of yesterday's-and-today's surface directories.

Surfaces (thinking-Alice's voiced thoughts) live flat under
``inner/surface/<id>.md``. The runtime also dates them via
``inner/surface/{today,yesterday}/`` subdirectories — the watcher
intentionally ignores those at runtime (flat-file dispatch only),
but we want a startup-time count so the daemon notices stranded
items the operator can manually re-flatten.

This is a deliberately lightweight implementation. The cortex-memory
research notes propose a richer "classify-by-priority and dispatch
flash items immediately" pipeline; that's a behaviour change worth
its own design pass and lands later. For now the source just counts
what it finds and exposes the count via ``ctx`` so prompts and
operators can see it.
"""

from __future__ import annotations

import logging
import pathlib

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


class SurfaceScanStartup:
    """Count stranded surfaces under ``inner/surface/{today,yesterday}/``.

    Sets ``ctx.startup_surface_backlog`` to the integer count so
    handlers / prompt builders can surface it. Missing directories
    are treated as zero (the trees are created lazily on first
    surface drop).
    """

    name = "surface_scan"

    def __init__(self, mind_dir: pathlib.Path) -> None:
        self._surface_dir = mind_dir / "inner" / "surface"

    async def run_once(self, ctx: DaemonContext) -> None:
        backlog = self._scan(self._surface_dir / "today") + self._scan(
            self._surface_dir / "yesterday"
        )
        # Stored on the daemon via the proxy; handlers and the
        # prompt-builder can read it without an import. None means
        # "scan didn't run yet"; 0 means "scan ran, found nothing."
        ctx.startup_surface_backlog = backlog
        if backlog > 0:
            log.info(
                "startup surface scan: %d stranded item(s) in "
                "today/ + yesterday/ — these will not auto-dispatch",
                backlog,
            )

    @staticmethod
    def _scan(dated_dir: pathlib.Path) -> int:
        if not dated_dir.is_dir():
            return 0
        try:
            return sum(
                1
                for path in dated_dir.glob("*.md")
                if not path.name.startswith(".")
            )
        except OSError as exc:
            log.warning("surface scan error in %s: %s", dated_dir, exc)
            return 0
