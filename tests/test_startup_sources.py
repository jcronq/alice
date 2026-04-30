"""Phase 5b of plan 01: StartupSource scaffolds.

Each startup source must satisfy two invariants:

1. When the source-of-truth file/directory it reads is missing,
   ``run_once(ctx)`` does not raise. The daemon's startup phase is
   best-effort; one missing mind file must not break boot.
2. When the file is present, ``run_once`` exposes its content (or
   a simple count) on ``ctx`` so the prompt-builder and handlers
   can consult it without re-opening the file.

The cortex-index freshness source has a third invariant: it
shells out to :mod:`alice_core.cortex_index` only when the vault
exists, and tolerates the kernel module raising on a malformed db.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace


from alice_speaking.startup import (
    CortexIndexFreshnessStartup,
    MesoStateStartup,
    PrebriefRegistryStartup,
    StartupSource,
    SurfaceScanStartup,
)


def _ctx() -> SimpleNamespace:
    """Stub for the DaemonContext proxy. Startup sources only set
    attributes on ctx; they don't call methods, so a SimpleNamespace
    is enough."""
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# Protocol conformance


def test_each_source_satisfies_startup_protocol(tmp_path):
    """Every concrete startup source must runtime_checkable as
    :class:`StartupSource`. Same recurrence guard as Phase 2's
    transport-protocol test, applied to startup."""
    sources = [
        SurfaceScanStartup(tmp_path),
        PrebriefRegistryStartup(tmp_path),
        MesoStateStartup(tmp_path),
        CortexIndexFreshnessStartup(tmp_path),
    ]
    for s in sources:
        assert isinstance(s, StartupSource), f"{type(s).__name__} not a StartupSource"


# ---------------------------------------------------------------------------
# Surface scan


def test_surface_scan_counts_stranded_items(tmp_path: pathlib.Path):
    today = tmp_path / "inner" / "surface" / "today"
    yesterday = tmp_path / "inner" / "surface" / "yesterday"
    today.mkdir(parents=True)
    yesterday.mkdir(parents=True)
    (today / "a.md").write_text("a")
    (today / "b.md").write_text("b")
    (yesterday / "c.md").write_text("c")
    (today / ".dot.md").write_text("hidden")  # ignored

    src = SurfaceScanStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert ctx.startup_surface_backlog == 3


def test_surface_scan_handles_missing_dirs(tmp_path: pathlib.Path):
    src = SurfaceScanStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert ctx.startup_surface_backlog == 0


# ---------------------------------------------------------------------------
# Prebrief registry


def test_prebrief_registry_loads_text_when_present(tmp_path: pathlib.Path):
    f = tmp_path / "memory" / "fitness" / "PHASE1-PREBRIEF-REGISTRY.md"
    f.parent.mkdir(parents=True)
    f.write_text("# entries\n- 2026-04-30: tempo run\n")

    src = PrebriefRegistryStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert ctx.prebrief_registry is not None
    assert "tempo run" in ctx.prebrief_registry


def test_prebrief_registry_silent_when_missing(tmp_path: pathlib.Path):
    src = PrebriefRegistryStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert ctx.prebrief_registry is None


# ---------------------------------------------------------------------------
# Meso state


def test_meso_state_loads_text_when_present(tmp_path: pathlib.Path):
    f = tmp_path / "memory" / "fitness" / "MESO-STATE.md"
    f.parent.mkdir(parents=True)
    f.write_text("week: 3\nphase: hypertrophy\n")

    src = MesoStateStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert ctx.meso_state is not None
    assert "hypertrophy" in ctx.meso_state


def test_meso_state_silent_when_missing(tmp_path: pathlib.Path):
    src = MesoStateStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert ctx.meso_state is None


# ---------------------------------------------------------------------------
# Cortex index freshness


def test_cortex_index_skips_when_vault_missing(tmp_path: pathlib.Path):
    """No vault → no rebuild attempt, no exception."""
    src = CortexIndexFreshnessStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))  # must not raise


def test_cortex_index_calls_kernel_when_vault_present(
    tmp_path: pathlib.Path, monkeypatch
):
    """Vault present → asks kernel ``needs_rebuild``; if True,
    calls ``build``. Both monkeypatched so the test doesn't touch
    the FTS5 sqlite layer."""
    vault = tmp_path / "cortex-memory"
    vault.mkdir()
    (vault / "note.md").write_text("hello")

    rebuild_calls: list[tuple[pathlib.Path, pathlib.Path]] = []
    needs_calls: list[tuple[pathlib.Path, pathlib.Path]] = []

    import alice_indexer.build_index as bi

    def fake_needs(v, db, **kw):
        needs_calls.append((v, db))
        return True

    def fake_build(v, db):
        rebuild_calls.append((v, db))
        return {"records": 1, "duration_ms": 0}

    monkeypatch.setattr(bi, "needs_rebuild", fake_needs)
    monkeypatch.setattr(bi, "build", fake_build)

    src = CortexIndexFreshnessStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))

    assert len(needs_calls) == 1
    assert needs_calls[0][0] == vault
    assert len(rebuild_calls) == 1


def test_cortex_index_skips_rebuild_when_fresh(
    tmp_path: pathlib.Path, monkeypatch
):
    vault = tmp_path / "cortex-memory"
    vault.mkdir()

    rebuild_called = False

    import alice_indexer.build_index as bi

    def fake_needs(v, db, **kw):
        return False

    def fake_build(v, db):
        nonlocal rebuild_called
        rebuild_called = True
        return {}

    monkeypatch.setattr(bi, "needs_rebuild", fake_needs)
    monkeypatch.setattr(bi, "build", fake_build)

    src = CortexIndexFreshnessStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))
    assert not rebuild_called


def test_cortex_index_swallows_oserror_from_check(
    tmp_path: pathlib.Path, monkeypatch
):
    """Kernel can hit OSError on a malformed db. We log + skip,
    don't crash the daemon."""
    vault = tmp_path / "cortex-memory"
    vault.mkdir()

    import alice_indexer.build_index as bi

    def fake_needs(*a, **kw):
        raise OSError("bad sqlite")

    monkeypatch.setattr(bi, "needs_rebuild", fake_needs)

    src = CortexIndexFreshnessStartup(tmp_path)
    ctx = _ctx()
    asyncio.run(src.run_once(ctx))  # must not raise
