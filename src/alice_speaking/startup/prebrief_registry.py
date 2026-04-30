"""Read the fitness prebrief registry into ``ctx`` at startup.

``memory/fitness/PHASE1-PREBRIEF-REGISTRY.md`` is a per-user mind
file the operator (Alice's owner) edits to track Phase-1 fitness
prebriefs that are due. The runtime doesn't parse the markdown —
the format is human-driven and the meaning lives in Alice's prompts
— but we surface the file's presence + raw text on ``ctx`` so the
prompt-builder can include it when relevant.

Fail-soft: when the file doesn't exist (most users won't have one),
we silently set the registry text to ``None`` and move on. This is
explicitly what the plan calls "best-effort startup."
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


class PrebriefRegistryStartup:
    """Load ``memory/fitness/PHASE1-PREBRIEF-REGISTRY.md`` into
    ``ctx.prebrief_registry`` (or ``None`` when missing)."""

    name = "prebrief_registry"

    def __init__(self, mind_dir: pathlib.Path) -> None:
        self._path = mind_dir / "memory" / "fitness" / "PHASE1-PREBRIEF-REGISTRY.md"

    async def run_once(self, ctx: DaemonContext) -> None:
        text: Optional[str]
        if self._path.is_file():
            try:
                text = self._path.read_text()
            except OSError as exc:
                log.warning("prebrief registry read failed: %s", exc)
                text = None
            else:
                log.info(
                    "loaded prebrief registry (%d chars) from %s",
                    len(text),
                    self._path,
                )
        else:
            text = None
        ctx.prebrief_registry = text
