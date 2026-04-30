"""Build the dispatcher's :class:`SourceRegistry` from a :class:`Config`.

Phase 5c of plan 01: the gating logic that decides "is Discord
configured?" / "is A2A enabled?" used to live inline in
``SpeakingDaemon.__init__``. Pulling it out here keeps that logic
in one place and gives tests a way to construct a registry without
the rest of the daemon scaffolding.

The factory does NOT instantiate the transport classes themselves —
``SpeakingDaemon`` still owns the lifecycle of every ``SignalClient``
/ ``CLITransport`` / ``DiscordTransport`` / ``A2ATransport`` because
those need access to the daemon's send-message closure, dedup store,
address book, etc. What this module owns is the shape:

- which startup sources to enqueue (always all four — they're
  individually fail-soft)
- which transports + internal sources to register, given a list of
  the ones that have actually been constructed.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from alice_prompts import DEFAULTS_DIR as PROMPT_DEFAULTS_DIR
from alice_prompts import PromptLoader

from .infra.config import Config
from .internal import EmergencyWatcher, SurfaceWatcher
from .startup import (
    CortexIndexFreshnessStartup,
    MesoStateStartup,
    PrebriefRegistryStartup,
    SurfaceScanStartup,
)
from .transports import SourceRegistry, Transport


log = logging.getLogger(__name__)


def build_prompt_loader(cfg: Config) -> PromptLoader:
    """Construct a :class:`PromptLoader` wired with this mind's
    override path.

    The override path is ``mind/.alice/prompts/`` — populated by
    ``alice-init`` when the operator scaffolds a fresh mind (Plan 04
    Phase 7). If it doesn't exist (older minds, custom paths), the
    loader silently falls back to the runtime defaults bundled with
    the wheel.

    The persona placeholder context is the same stand-in the package-
    level loader uses; Plan 05 will replace it with real personae
    drawn from the mind.
    """
    return PromptLoader(
        defaults_path=PROMPT_DEFAULTS_DIR,
        override_path=cfg.mind_dir / ".alice" / "prompts",
        context_defaults={
            "agent": {"name": "Alice"},
            "user": {"name": "the operator"},
        },
    )


def build_registry(
    cfg: Config,
    *,
    transports: Iterable[Optional[Transport]],
    surface_watcher: SurfaceWatcher,
    emergency_watcher: EmergencyWatcher,
) -> SourceRegistry:
    """Assemble a :class:`SourceRegistry` for one daemon session.

    Args:
        cfg: Resolved :class:`Config`. Used to wire startup sources
            against ``cfg.mind_dir``.
        transports: Iterable of :class:`Transport` instances that the
            daemon has already constructed. ``None`` entries (e.g. a
            disabled Signal/Discord/A2A) are skipped — the caller
            doesn't need to filter. Signal is intentionally NOT
            registered (Phase 2a invariant), so callers should
            simply omit it from this iterable.
        surface_watcher / emergency_watcher: the daemon-owned
            :class:`InternalSource` instances; injected rather than
            constructed here so the daemon can also reach them
            directly for archive bookkeeping.
    """
    registry = SourceRegistry()
    for transport in transports:
        if transport is None:
            continue
        registry.register(transport)
    registry.register_internal(surface_watcher)
    registry.register_internal(emergency_watcher)
    # Startup sources are individually fail-soft — register them
    # all. The dispatcher's startup phase iterates ``all_startup_sources``
    # and runs each, swallowing exceptions per-source.
    for source in (
        SurfaceScanStartup(cfg.mind_dir),
        PrebriefRegistryStartup(cfg.mind_dir),
        MesoStateStartup(cfg.mind_dir),
        CortexIndexFreshnessStartup(cfg.mind_dir),
    ):
        registry.register_startup(source)
    return registry


async def run_startup_phase(registry: SourceRegistry, ctx) -> None:
    """Execute every registered :class:`StartupSource` once.

    Failures are logged per-source and don't propagate — the daemon
    must still boot when one mind file is missing or one helper
    barfs. Each source's ``run_once`` is awaited sequentially so
    later sources can read state earlier ones primed on ``ctx``.
    """
    for source in registry.all_startup_sources():
        try:
            await source.run_once(ctx)
        except Exception:  # noqa: BLE001
            log.exception(
                "startup source %s failed; continuing", source.name
            )
