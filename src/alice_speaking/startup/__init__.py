"""Startup sources — once-per-session tasks the daemon runs before
entering the dispatcher loop.

Sibling of :mod:`alice_speaking.transports` and
:mod:`alice_speaking.internal`, but with a different shape: a startup
source has no producer (it doesn't push events onto the queue) and
no event_type. It runs once via ``run_once(ctx)``, completes, and
returns; failures are logged but don't block the daemon from
starting.

Phase 5 of plan 01 ships four concrete sources, all fail-soft so a
fresh install with no mind data still boots:

- :class:`SurfaceScanStartup` — count stranded items in
  ``inner/surface/{today,yesterday}/``.
- :class:`PrebriefRegistryStartup` — load
  ``memory/fitness/PHASE1-PREBRIEF-REGISTRY.md`` (when present).
- :class:`MesoStateStartup` — load ``memory/fitness/MESO-STATE.md``
  (when present).
- :class:`CortexIndexFreshnessStartup` — rebuild the FTS index
  via ``alice_core.cortex_index`` if stale.

Plan 04's cue runner will add a ``CortexL1Startup`` here once the
prompt-template loader lands.
"""

from .base import StartupSource
from .cortex_index_freshness import CortexIndexFreshnessStartup
from .meso_state import MesoStateStartup
from .prebrief_registry import PrebriefRegistryStartup
from .surface_scan import SurfaceScanStartup

__all__ = [
    "CortexIndexFreshnessStartup",
    "MesoStateStartup",
    "PrebriefRegistryStartup",
    "StartupSource",
    "SurfaceScanStartup",
]
