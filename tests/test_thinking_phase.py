"""Tests for ``alice_thinking.phase`` — selector + vault snapshot + loader.

Design: ``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``.

Pin the Phase 0 contract (default config preserves today's behavior),
the Phase 3 cascade (full B/C/D dispatch under
``enable_full_sleep_dispatch=True``), and the bit-identical
expectation for ``PromptFragmentLoader.compose()``.
"""

from __future__ import annotations

import datetime as _dt
import pathlib

import pytest

from alice_thinking.phase import (
    Phase,
    PhaseConfig,
    PromptFragmentLoader,
    VaultSnapshot,
    build_vault_snapshot,
    detect_commission_notes,
    select_phase,
)


def _snap(
    *,
    hour: int = 14,
    minute: int = 0,
    has_inbox_items: bool = False,
    has_broken_links: bool = False,
    has_orphan_stubs: bool = False,
    has_recent_research: bool = False,
    consecutive_b: int = 0,
    consecutive_null_c: int = 0,
    stage_d_cap_exhausted: bool = False,
    state_dir: pathlib.Path = pathlib.Path("/tmp/state"),
    today: str = "2026-05-07",
) -> VaultSnapshot:
    return VaultSnapshot(
        hour=hour,
        minute=minute,
        has_inbox_items=has_inbox_items,
        has_broken_links=has_broken_links,
        has_orphan_stubs=has_orphan_stubs,
        has_recent_research=has_recent_research,
        consecutive_b=consecutive_b,
        consecutive_null_c=consecutive_null_c,
        stage_d_cap_exhausted=stage_d_cap_exhausted,
        vault_dir_mtime=0.0,
        state_dir=state_dir,
        today=today,
    )


# ---------------------------------------------------------------------------
# select_phase — Phase 0 contract (default config)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [7, 8, 12, 16, 22])
def test_default_config_returns_active_during_day(hour: int) -> None:
    assert select_phase(_snap(hour=hour)) is Phase.ACTIVE


@pytest.mark.parametrize("hour", [23, 0, 1, 3, 5, 6])
def test_default_config_collapses_sleep_to_b(hour: int) -> None:
    """Phase 0 contract: default config keeps sleep dispatch collapsed
    to SLEEP_B, ignoring vault state. Phase 3 unlocks B/C/D."""
    snap = _snap(
        hour=hour,
        has_inbox_items=False,
        has_recent_research=True,
        consecutive_b=10,
    )
    assert select_phase(snap) is Phase.SLEEP_B


def test_default_config_active_window_endpoints() -> None:
    """07:00 active, 22:59 active, 23:00 sleep, 06:59 sleep."""
    assert select_phase(_snap(hour=7)) is Phase.ACTIVE
    assert select_phase(_snap(hour=22)) is Phase.ACTIVE
    assert select_phase(_snap(hour=23)) is Phase.SLEEP_B
    assert select_phase(_snap(hour=6)) is Phase.SLEEP_B


def test_quick_mode_short_circuits() -> None:
    cfg = PhaseConfig(quick_mode=True)
    # Even at sleep hour with a backed-up vault, quick wins.
    snap = _snap(hour=2, has_inbox_items=True)
    assert select_phase(snap, cfg) is Phase.QUICK


# ---------------------------------------------------------------------------
# select_phase — Phase 3 cascade (full dispatch)
# ---------------------------------------------------------------------------


def _full_cfg() -> PhaseConfig:
    return PhaseConfig(enable_full_sleep_dispatch=True)


def test_full_dispatch_inbox_wins() -> None:
    snap = _snap(hour=2, has_inbox_items=True, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


def test_full_dispatch_broken_links_route_to_b() -> None:
    snap = _snap(hour=4, has_broken_links=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


def test_full_dispatch_consecutive_b_loop_breaks_to_c_when_no_corpus() -> None:
    snap = _snap(hour=1, consecutive_b=6, has_recent_research=False)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_full_dispatch_consecutive_b_loop_breaks_to_d_when_corpus_exists() -> None:
    snap = _snap(hour=1, consecutive_b=6, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_full_dispatch_consecutive_b_skips_d_when_cap_exhausted() -> None:
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=True,
        stage_d_cap_exhausted=True,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_full_dispatch_early_phase_default_is_c() -> None:
    snap = _snap(hour=23, consecutive_null_c=0)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_full_dispatch_early_null_c_loop_escapes_to_d() -> None:
    snap = _snap(
        hour=0,
        consecutive_null_c=6,
        has_recent_research=True,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_full_dispatch_late_phase_with_corpus_is_d() -> None:
    snap = _snap(hour=4, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_full_dispatch_late_phase_without_corpus_falls_back_to_b() -> None:
    snap = _snap(hour=5, has_recent_research=False)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


# ---------------------------------------------------------------------------
# build_vault_snapshot — filesystem probe
# ---------------------------------------------------------------------------


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 5, 7, 14, 0)


def test_build_vault_snapshot_handles_missing_mind_dir(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.has_inbox_items is False
    assert snap.has_broken_links is False
    assert snap.consecutive_b == 0
    assert snap.consecutive_null_c == 0
    assert snap.stage_d_cap_exhausted is False


def test_build_vault_snapshot_detects_inbox_items(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    (notes / "fresh.md").write_text("hi")
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.has_inbox_items is True


def test_build_vault_snapshot_ignores_consumed_dir(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    consumed = notes / ".consumed" / "2026-05-06"
    consumed.mkdir(parents=True)
    (consumed / "x.md").write_text("processed")
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.has_inbox_items is False


def test_build_vault_snapshot_reads_consecutive_counters(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    today = _now().date().isoformat()
    (state_dir / f"consecutive-b-{today}.txt").write_text("4\n")
    (state_dir / f"consecutive-null-c-{today}.txt").write_text("2\n")
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.consecutive_b == 4
    assert snap.consecutive_null_c == 2


def test_build_vault_snapshot_detects_stage_d_cap_exhaustion(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    today = _now().date().isoformat()
    pairs = state_dir / f"stage-d-pairs-{today}.jsonl"
    pairs.write_text(
        "\n".join(
            f'{{"ts": "x", "synthesis": "s{i}"}}' for i in range(3)
        )
        + "\n"
    )
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.stage_d_cap_exhausted is True


def test_build_vault_snapshot_recent_research_window(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    research = tmp_path / "cortex-memory" / "research"
    research.mkdir(parents=True)
    (research / "a.md").write_text("a")
    (research / "b.md").write_text("b")
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.has_recent_research is True


# ---------------------------------------------------------------------------
# PromptFragmentLoader — composition
# ---------------------------------------------------------------------------


def test_loader_compose_prelude_and_phase_fragment() -> None:
    loader = PromptFragmentLoader()
    out = loader.compose(Phase.ACTIVE, timestamp_header="Current local time: 2026-05-07 14:00 EDT (Thursday)")
    # Header is at the top.
    assert out.startswith("Current local time: 2026-05-07 14:00 EDT (Thursday)")
    # Prelude content present.
    assert "Thinking Alice — wake" in out
    assert "Step 1 — write the wake file" in out
    # Phase fragment present.
    assert "Step 0 — active mode" in out


def test_loader_each_phase_has_unique_fragment() -> None:
    loader = PromptFragmentLoader()
    header = "Current local time: 2026-05-07 02:00 EDT (Thursday)"
    a = loader.compose(Phase.ACTIVE, timestamp_header=header)
    b = loader.compose(Phase.SLEEP_B, timestamp_header=header)
    c = loader.compose(Phase.SLEEP_C, timestamp_header=header)
    d = loader.compose(Phase.SLEEP_D, timestamp_header=header)
    assert a != b != c != d
    # Sanity: phase-specific Step 0 strings.
    assert "Step 0 — active mode" in a
    assert "Stage B (Consolidation)" in b
    assert "Stage C (Downscaling" in c
    assert "Stage D (Recombination" in d


def test_loader_quick_phase_is_not_composable() -> None:
    """Phase.QUICK keeps its own minimal prompt — not a composed fragment."""
    loader = PromptFragmentLoader()
    with pytest.raises(ValueError):
        loader.load_phase(Phase.QUICK)


def test_loader_compose_threads_injected_content() -> None:
    """Forward-compat seam: the ``injected_content`` kwarg is plumbed
    for the STM/LTM design. Today it should appear between prelude
    and phase body."""
    loader = PromptFragmentLoader()
    out = loader.compose(
        Phase.ACTIVE,
        timestamp_header="ts",
        injected_content="--- STM SNAPSHOT ---",
    )
    assert "--- STM SNAPSHOT ---" in out
    # Order: prelude → injected → phase fragment.
    prelude_idx = out.index("Thinking Alice — wake")
    inject_idx = out.index("--- STM SNAPSHOT ---")
    phase_idx = out.index("Step 0 — active mode")
    assert prelude_idx < inject_idx < phase_idx


# ---------------------------------------------------------------------------
# detect_commission_notes
# ---------------------------------------------------------------------------


def test_detect_commission_notes_frontmatter(tmp_path: pathlib.Path) -> None:
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    (notes / "regular.md").write_text("---\nkind: chat\n---\n")
    target = notes / "needs-design.md"
    target.write_text(
        "---\ntask_type: design-commission\nslug: foo\n---\n\nbody\n"
    )
    found = detect_commission_notes(tmp_path)
    assert found == [target]


def test_detect_commission_notes_filename_fallback(tmp_path: pathlib.Path) -> None:
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    target = notes / "2026-05-07-design-commission-foo.md"
    target.write_text("body")
    found = detect_commission_notes(tmp_path)
    assert target in found


def test_detect_commission_notes_folder_fallback(tmp_path: pathlib.Path) -> None:
    notes = tmp_path / "inner" / "notes" / ".design-commissions"
    notes.mkdir(parents=True)
    target = notes / "thing.md"
    target.write_text("body")
    found = detect_commission_notes(tmp_path)
    assert target in found


def test_detect_commission_notes_sorted_oldest_first(
    tmp_path: pathlib.Path,
) -> None:
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    a = notes / "a-design-commission.md"
    a.write_text("a")
    b = notes / "b-design-commission.md"
    b.write_text("b")
    import os

    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    found = detect_commission_notes(tmp_path)
    assert found == [a, b]


# ---------------------------------------------------------------------------
# Phase 0 success criterion — semantic equivalence
# ---------------------------------------------------------------------------


def test_compose_includes_constitutional_boundary_in_every_phase() -> None:
    """The constitutional boundary lives in the prelude so it must
    appear in the composed prompt for every phase."""
    loader = PromptFragmentLoader()
    header = "ts"
    sentinel = "Constitutional boundary"
    for phase in (Phase.ACTIVE, Phase.SLEEP_B, Phase.SLEEP_C, Phase.SLEEP_D):
        out = loader.compose(phase, timestamp_header=header)
        assert sentinel in out, f"{phase} missing prelude sentinel"


def test_compose_carries_step_5_close_in_every_phase() -> None:
    """Step 5 (close + prune) is shared — must appear in every phase
    composition. Pin so a future split can't accidentally drop it."""
    loader = PromptFragmentLoader()
    header = "ts"
    for phase in (Phase.ACTIVE, Phase.SLEEP_B, Phase.SLEEP_C, Phase.SLEEP_D):
        out = loader.compose(phase, timestamp_header=header)
        assert "Step 5 — close clean" in out
