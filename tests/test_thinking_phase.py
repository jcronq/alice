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
    detect_conflict_notes,
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
# select_phase — Phase 3 default (full B/C/D cascade enabled)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [7, 8, 12, 16, 22])
def test_default_config_returns_active_during_day(hour: int) -> None:
    assert select_phase(_snap(hour=hour)) is Phase.ACTIVE


def test_default_config_active_window_endpoints() -> None:
    """07:00 active, 22:59 active. Sleep window split between B/C/D
    once Phase 3 default fires."""
    assert select_phase(_snap(hour=7)) is Phase.ACTIVE
    assert select_phase(_snap(hour=22)) is Phase.ACTIVE


def test_quick_mode_short_circuits() -> None:
    cfg = PhaseConfig(quick_mode=True)
    # Even at sleep hour with a backed-up vault, quick wins.
    snap = _snap(hour=2, has_inbox_items=True)
    assert select_phase(snap, cfg) is Phase.QUICK


def test_default_config_full_dispatch_is_on_by_default() -> None:
    """Phase 3 default contract: ``enable_full_sleep_dispatch`` is on.
    Sleep wakes route to B/C/D from vault state. Inbox-or-issues
    rule wins → B; clean vault, late hour, fresh research → D."""
    # 02:00 with inbox items: Rule 2a fires.
    assert select_phase(_snap(hour=2, has_inbox_items=True)) is Phase.SLEEP_B
    # 04:00 clean vault, fresh research: Rule 2d fires.
    assert select_phase(_snap(hour=4, has_recent_research=True)) is Phase.SLEEP_D


def test_kill_switch_collapses_sleep_to_b() -> None:
    """Setting ``enable_full_sleep_dispatch=False`` (the Phase 0
    behavior) collapses every sleep wake to ``SLEEP_B`` regardless
    of vault state. Kept as a config override for emergencies."""
    cfg = PhaseConfig(enable_full_sleep_dispatch=False)
    snap = _snap(
        hour=4,
        has_inbox_items=False,
        has_recent_research=True,
        consecutive_b=10,
    )
    assert select_phase(snap, cfg) is Phase.SLEEP_B


# ---------------------------------------------------------------------------
# select_phase — full B/C/D cascade (six rules + fallback)
#
# Rules under ``enable_full_sleep_dispatch=True``:
#   - Rule 2a: inbox / broken-links / orphan-stubs   → SLEEP_B
#   - Rule 2b: 6+ consecutive Stage B wakes          → SLEEP_C / D
#   - Rule 2c: 23:00–02:59 default                   → SLEEP_C (with null-C escape to D)
#   - Rule 2d: 03:00–06:59 with research corpus     → SLEEP_D
#   - Fallback: late phase, no corpus                → SLEEP_B
# ---------------------------------------------------------------------------


def _full_cfg() -> PhaseConfig:
    """Explicit full-dispatch config — same as the default, but
    repeated here so the cascade tests document the rule under test."""
    return PhaseConfig(enable_full_sleep_dispatch=True)


# Rule 2a — inbox / broken links / orphan stubs always win
def test_rule_2a_inbox_wins() -> None:
    snap = _snap(hour=2, has_inbox_items=True, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


def test_rule_2a_broken_links_route_to_b() -> None:
    snap = _snap(hour=4, has_broken_links=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


def test_rule_2a_orphan_stubs_route_to_b() -> None:
    snap = _snap(hour=5, has_orphan_stubs=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


def test_rule_2a_inbox_beats_consecutive_b_loop_break() -> None:
    """Inbox always wins, even when the loop-break threshold is met."""
    snap = _snap(
        hour=1,
        has_inbox_items=True,
        consecutive_b=10,
        has_recent_research=True,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


# Rule 2b — consecutive-B threshold breaks the loop
def test_rule_2b_consecutive_b_loop_breaks_to_c_when_no_corpus() -> None:
    snap = _snap(hour=1, consecutive_b=6, has_recent_research=False)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2b_consecutive_b_loop_breaks_to_d_when_corpus_exists() -> None:
    snap = _snap(hour=1, consecutive_b=6, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2b_consecutive_b_skips_d_when_cap_exhausted() -> None:
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=True,
        stage_d_cap_exhausted=True,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2b_threshold_is_configurable() -> None:
    """``consecutive_b_threshold`` is tunable from config."""
    cfg = PhaseConfig(enable_full_sleep_dispatch=True, consecutive_b_threshold=3)
    snap = _snap(hour=1, consecutive_b=3, has_recent_research=False)
    assert select_phase(snap, cfg) is Phase.SLEEP_C


# Rule 2c — early phase (23:00–02:59) defaults to C
def test_rule_2c_early_phase_default_is_c() -> None:
    snap = _snap(hour=23, consecutive_null_c=0)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


@pytest.mark.parametrize("hour", [23, 0, 1, 2])
def test_rule_2c_covers_full_early_window(hour: int) -> None:
    snap = _snap(hour=hour)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2c_early_null_c_loop_escapes_to_d() -> None:
    snap = _snap(
        hour=0,
        consecutive_null_c=6,
        has_recent_research=True,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2c_null_c_threshold_blocked_by_cap() -> None:
    snap = _snap(
        hour=0,
        consecutive_null_c=6,
        has_recent_research=True,
        stage_d_cap_exhausted=True,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2c_null_c_threshold_blocked_without_corpus() -> None:
    snap = _snap(hour=1, consecutive_null_c=6, has_recent_research=False)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


# Rule 2d — late phase (03:00–06:59) with corpus → D, else fallback
def test_rule_2d_late_phase_with_corpus_is_d() -> None:
    snap = _snap(hour=4, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


@pytest.mark.parametrize("hour", [3, 4, 5, 6])
def test_rule_2d_covers_full_late_window(hour: int) -> None:
    snap = _snap(hour=hour, has_recent_research=True)
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2d_skipped_when_cap_exhausted() -> None:
    snap = _snap(
        hour=4,
        has_recent_research=True,
        stage_d_cap_exhausted=True,
    )
    # Cap exhausted → falls through to the trailing fallback (SLEEP_B).
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


# Fallback — late phase, no corpus → B
def test_fallback_late_phase_without_corpus_is_b() -> None:
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


# ---------------------------------------------------------------------------
# detect_conflict_notes — vault-state-driven conflict resolution preempt.
#
# Speaking review 2026-05-07: conflict resolution mirrors design commission
# as a task-type-triggered phase. ``select_phase()`` is NOT modified — the
# preempt is task-detected in ``wake.py``, not cadence-dispatched.
# ---------------------------------------------------------------------------


def test_detect_conflict_notes_returns_open_items(tmp_path: pathlib.Path) -> None:
    """An ``open`` conflict (or one with no status field) is returned."""
    conflicts = tmp_path / "conflicts"
    conflicts.mkdir(parents=True)

    open_explicit = conflicts / "alpha.md"
    open_explicit.write_text("---\nstatus: open\n---\n\nbody\n")

    no_status = conflicts / "beta.md"
    no_status.write_text("---\ntopic: weight\n---\n\nbody\n")

    found = detect_conflict_notes(tmp_path)
    assert open_explicit in found
    assert no_status in found


def test_detect_conflict_notes_ignores_resolved(tmp_path: pathlib.Path) -> None:
    """Conflicts with ``status: resolved`` (or anything other than
    ``open``) are excluded."""
    conflicts = tmp_path / "conflicts"
    conflicts.mkdir(parents=True)

    open_one = conflicts / "open.md"
    open_one.write_text("---\nstatus: open\n---\n")
    resolved = conflicts / "resolved.md"
    resolved.write_text("---\nstatus: resolved\n---\n")

    found = detect_conflict_notes(tmp_path)
    assert open_one in found
    assert resolved not in found


def test_detect_conflict_notes_ignores_resolved_subdir(
    tmp_path: pathlib.Path,
) -> None:
    """Files under ``conflicts/.resolved/`` are excluded — that's the
    archive folder for items already worked through."""
    conflicts = tmp_path / "conflicts"
    archive = conflicts / ".resolved"
    archive.mkdir(parents=True)
    (archive / "old.md").write_text("---\nstatus: open\n---\n")

    found = detect_conflict_notes(tmp_path)
    assert found == []


def test_detect_conflict_notes_handles_missing_dir(tmp_path: pathlib.Path) -> None:
    """No ``conflicts/`` directory → empty list (no crash)."""
    assert detect_conflict_notes(tmp_path) == []


def test_detect_conflict_notes_sorted_oldest_first(tmp_path: pathlib.Path) -> None:
    conflicts = tmp_path / "conflicts"
    conflicts.mkdir(parents=True)
    a = conflicts / "a.md"
    a.write_text("---\nstatus: open\n---\n")
    b = conflicts / "b.md"
    b.write_text("---\nstatus: open\n---\n")

    import os

    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    found = detect_conflict_notes(tmp_path)
    assert found == [a, b]


def test_select_phase_does_not_dispatch_to_conflict_resolution() -> None:
    """``Phase.CONFLICT_RESOLUTION`` is task-detected (mirrors
    ``DESIGN_COMMISSION``), not cadence-dispatched. The selector
    must never return it from any vault snapshot — the preempt
    lives in ``wake.py``.
    """
    # Sweep representative snapshots across the day; none should
    # produce CONFLICT_RESOLUTION.
    snaps = [
        _snap(hour=h)
        for h in (0, 1, 4, 7, 12, 16, 22, 23)
    ] + [
        _snap(hour=2, has_inbox_items=True),
        _snap(hour=4, has_recent_research=True),
        _snap(hour=1, consecutive_b=10, has_recent_research=True),
    ]
    for snap in snaps:
        out = select_phase(snap)
        assert out is not Phase.CONFLICT_RESOLUTION
