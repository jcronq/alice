"""Read the fitness meso-cycle state file into ``ctx`` at startup.

``memory/fitness/MESO-STATE.md`` tracks the operator's current
mesocycle (training-block) week. Same shape as the prebrief
registry: human-edited markdown the runtime doesn't parse — we
just expose the raw text on ``ctx`` so the prompt-builder can
include it.

Fail-soft when the file doesn't exist.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


class MesoStateStartup:
    """Load ``memory/fitness/MESO-STATE.md`` into ``ctx.meso_state``
    (or ``None`` when missing)."""

    name = "meso_state"

    def __init__(self, mind_dir: pathlib.Path) -> None:
        self._path = mind_dir / "memory" / "fitness" / "MESO-STATE.md"

    async def run_once(self, ctx: DaemonContext) -> None:
        text: Optional[str]
        if self._path.is_file():
            try:
                text = self._path.read_text()
            except OSError as exc:
                log.warning("meso-state read failed: %s", exc)
                text = None
            else:
                log.info(
                    "loaded meso-state (%d chars) from %s",
                    len(text),
                    self._path,
                )
        else:
            text = None
        ctx.meso_state = text
