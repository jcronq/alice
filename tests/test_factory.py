"""Phase 5c of plan 01: registry construction + startup phase.

The factory:

- skips ``None`` transports (caller doesn't pre-filter)
- registers the surface + emergency watchers passed in
- enqueues all four startup sources

The startup phase:

- runs every registered source exactly once
- swallows + logs per-source exceptions so one failure doesn't
  block boot
- runs sources in registration order (later sources can read
  state set by earlier ones)
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from alice_speaking.factory import build_registry, run_startup_phase
from alice_speaking.internal import EmergencyWatcher, SurfaceWatcher
from alice_speaking.transports.cli import CLIEvent, CLITransport


@pytest.fixture
def watchers(tmp_path: pathlib.Path):
    return SurfaceWatcher(tmp_path), EmergencyWatcher(tmp_path)


def test_build_registry_skips_none_transports(cfg, watchers):
    surf, emer = watchers
    registry = build_registry(
        cfg,
        transports=(None, None, None),
        surface_watcher=surf,
        emergency_watcher=emer,
    )
    # Internal sources still registered.
    assert registry.lookup(surf.event_type) is surf
    assert registry.lookup(emer.event_type) is emer
    # No transport events registered.
    assert registry.lookup(CLIEvent) is None


def test_build_registry_registers_provided_transports(cfg, watchers, tmp_path):
    surf, emer = watchers
    cli = CLITransport(socket_path=tmp_path / "alice.sock")
    registry = build_registry(
        cfg,
        transports=(cli, None, None),
        surface_watcher=surf,
        emergency_watcher=emer,
    )
    assert registry.lookup(CLIEvent) is cli


def test_build_registry_enqueues_four_startup_sources(cfg, watchers):
    surf, emer = watchers
    registry = build_registry(
        cfg,
        transports=(),
        surface_watcher=surf,
        emergency_watcher=emer,
    )
    sources = list(registry.all_startup_sources())
    names = [s.name for s in sources]
    assert names == [
        "surface_scan",
        "prebrief_registry",
        "meso_state",
        "cortex_index_freshness",
    ]


def test_run_startup_phase_calls_each_source_in_order(cfg, watchers):
    surf, emer = watchers

    calls: list[str] = []

    class _FakeSource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run_once(self, ctx) -> None:
            calls.append(self.name)
            setattr(ctx, f"flag_{self.name}", True)

    # Build a fresh registry rather than the factory-built one so
    # we can substitute the real startup sources for fakes that
    # record their call order.
    from alice_speaking.transports import SourceRegistry

    fresh = SourceRegistry()
    fresh.register_internal(surf)
    fresh.register_internal(emer)
    for name in ("a", "b", "c"):
        fresh.register_startup(_FakeSource(name))

    ctx = SimpleNamespace()
    asyncio.run(run_startup_phase(fresh, ctx))

    assert calls == ["a", "b", "c"]
    assert ctx.flag_a and ctx.flag_b and ctx.flag_c


def test_run_startup_phase_swallows_per_source_failures(cfg, watchers, caplog):
    surf, emer = watchers

    class _Boom:
        name = "boom"

        async def run_once(self, ctx):
            raise RuntimeError("boom")

    class _Ok:
        name = "ok"

        async def run_once(self, ctx):
            ctx.ok_ran = True

    from alice_speaking.transports import SourceRegistry

    registry = SourceRegistry()
    registry.register_internal(surf)
    registry.register_internal(emer)
    registry.register_startup(_Boom())
    registry.register_startup(_Ok())

    ctx = SimpleNamespace()
    with caplog.at_level("ERROR"):
        asyncio.run(run_startup_phase(registry, ctx))

    # Failure swallowed, log emitted, downstream source still ran.
    assert any("boom" in r.message for r in caplog.records)
    assert getattr(ctx, "ok_ran", False) is True
