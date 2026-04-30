"""Rebuild the cortex-memory FTS index at startup if it's stale.

``alice_core.cortex_index.build_index`` exposes ``needs_rebuild``
(the same predicate ``--check`` uses) and ``build`` (the rebuild
itself). At session start we ask the predicate; if the index is
stale we rebuild in-process, otherwise we skip with a debug log.

Fail-soft. A missing vault directory just means the index never
rebuilds — Alice still talks, the index just stays empty until
the operator drops notes into it.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


# 24h matches the default in ``build_index.needs_rebuild``. Lifted
# here so the operator can override per-deploy without forking the
# kernel module.
DEFAULT_MAX_STALE_SECONDS = 86_400


class CortexIndexFreshnessStartup:
    """Run ``cortex_index.needs_rebuild`` at startup; rebuild if stale.

    The vault and DB paths default to ``cfg.mind_dir/cortex-memory``
    and ``cfg.mind_dir/inner/state/cortex-index.db`` — matching the
    layout install.sh scaffolds. Override via constructor args when
    the deploy diverges.
    """

    name = "cortex_index_freshness"

    def __init__(
        self,
        mind_dir: pathlib.Path,
        *,
        vault: Optional[pathlib.Path] = None,
        db: Optional[pathlib.Path] = None,
        max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
    ) -> None:
        self._vault = vault or (mind_dir / "cortex-memory")
        self._db = db or (mind_dir / "inner" / "state" / "cortex-index.db")
        self._max_stale_seconds = max_stale_seconds

    async def run_once(self, ctx: DaemonContext) -> None:
        # Lazy import keeps the cortex_index module out of the
        # speaking import path when this startup source isn't wired.
        from alice_core.cortex_index.build_index import build, needs_rebuild

        if not self._vault.is_dir():
            log.debug(
                "cortex index freshness: vault %s missing; skipping",
                self._vault,
            )
            return
        try:
            stale = needs_rebuild(
                self._vault, self._db, max_stale_seconds=self._max_stale_seconds
            )
        except OSError as exc:
            log.warning(
                "cortex index freshness check failed (%s); skipping rebuild",
                exc,
            )
            return
        if not stale:
            log.debug("cortex index is fresh; no rebuild needed")
            return
        log.info(
            "cortex index stale; rebuilding from %s into %s",
            self._vault,
            self._db,
        )
        try:
            stats = build(self._vault, self._db)
        except OSError as exc:
            log.warning("cortex index rebuild failed (%s)", exc)
            return
        log.info("cortex index rebuilt: %s", stats)
