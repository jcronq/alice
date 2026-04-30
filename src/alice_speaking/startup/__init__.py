"""Startup sources — once-per-session tasks the daemon runs before
entering the dispatcher loop.

Sibling of :mod:`alice_speaking.transports` and
:mod:`alice_speaking.internal`, but with a different shape: a startup
source has no producer (it doesn't push events onto the queue) and
no event_type. It runs once via ``run_once(ctx)``, completes, and
returns; failures are logged but don't block the daemon from
starting.

Phase 5 of plan 01 will land the concrete sources designed in
``cortex-memory/research/2026-04-29-speaking-session-start-pipeline.md``:

- ``surface_scan.SurfaceScanStartup``
- ``prebrief_registry.PrebriefRegistryStartup``
- ``meso_state.MesoStateStartup``
- ``cortex_index_freshness.CortexIndexFreshnessStartup``
- ``cortex_l1.CortexL1Startup`` (deferred to plan 04)

Phase 2 ships only the protocol so the dispatcher can already type
its way around the seam.
"""

from .base import StartupSource

__all__ = ["StartupSource"]
