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
    CONFLICT_DEFER_THRESHOLD,
    STAGE_C_DEBT_ESCALATION_THRESHOLD,
    STAGE_D_NIGHTLY_CAP,
    Phase,
    PhaseConfig,
    PromptFragmentLoader,
    VaultSnapshot,
    _hours_since_last_d,
    build_vault_snapshot,
    detect_commission_notes,
    detect_conflict_notes,
    record_conflict_deferral,
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
    hours_since_last_d: float = 0.0,
    state_dir: pathlib.Path = pathlib.Path("/tmp/state"),
    today: str = "2026-05-07",
    stage_c_candidates_total: int = 0,
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
        hours_since_last_d=hours_since_last_d,
        vault_dir_mtime=0.0,
        state_dir=state_dir,
        today=today,
        stage_c_candidates_total=stage_c_candidates_total,
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
# Rules under ``enable_full_sleep_dispatch=True`` (evaluated in this order):
#   - Rule 2e: 4h+ since last D + corpus + cap free  → SLEEP_D (promoted 2026-05-25)
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


# ---------------------------------------------------------------------------
# Rule 2b — debt-weighted escalation (issue #388)
#
# When ``stage_c_candidates.total`` is at or above
# :data:`STAGE_C_DEBT_ESCALATION_THRESHOLD`, the consecutive-B loop break
# routes to Stage C even when a research corpus exists. Self-correcting:
# once C drains the debt below the threshold, the legacy corpus-driven
# D-preference resumes.
#
# Symptom that motivated the fix: Stage C ran zero times across 40
# sleep-phase wakes between Phase 3 deployment (2026-05-08) and the
# surface (2026-05-26) because the research corpus is non-empty every
# night, so Rule 2b always preferred D over C.
# ---------------------------------------------------------------------------


def test_rule_2b_debt_above_threshold_with_corpus_routes_to_c() -> None:
    """The fix: corpus is no longer enough to win over Stage C debt."""
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=True,
        stage_c_candidates_total=10,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2b_debt_below_threshold_with_corpus_routes_to_d() -> None:
    """Below the threshold, the prior corpus-driven D-preference is
    preserved — the fix is gated, not blanket."""
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=True,
        stage_c_candidates_total=2,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2b_debt_above_threshold_no_corpus_still_routes_to_c() -> None:
    """No-corpus path was already C; the new gate must not regress it."""
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=False,
        stage_c_candidates_total=10,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2b_debt_above_threshold_below_consecutive_b_falls_through() -> None:
    """The debt gate sits inside Rule 2b; if ``consecutive_b`` is below
    the loop-break threshold, the gate must not fire. Hour 1 with
    ``consecutive_b=0`` should land on Rule 2c (SLEEP_C) — same default
    as without the debt. Pin so a future refactor can't promote the
    debt gate above Rule 2a / 2e or out of the consecutive-B branch."""
    snap = _snap(
        hour=1,
        consecutive_b=0,
        has_recent_research=True,
        stage_c_candidates_total=20,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2b_debt_at_exact_threshold_routes_to_c() -> None:
    """Boundary: ``total == STAGE_C_DEBT_ESCALATION_THRESHOLD`` triggers
    the gate (``>=`` comparison)."""
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=True,
        stage_c_candidates_total=STAGE_C_DEBT_ESCALATION_THRESHOLD,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2b_debt_just_below_threshold_routes_to_d() -> None:
    """Boundary: ``total == STAGE_C_DEBT_ESCALATION_THRESHOLD - 1`` is
    not enough; corpus still wins."""
    snap = _snap(
        hour=1,
        consecutive_b=6,
        has_recent_research=True,
        stage_c_candidates_total=STAGE_C_DEBT_ESCALATION_THRESHOLD - 1,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2b_debt_threshold_constant_matches_design() -> None:
    """The design (issue #388) chose 5 to align with the Stage D nightly
    cap. Pin the constant so a future tune surfaces in code review."""
    assert STAGE_C_DEBT_ESCALATION_THRESHOLD == 5


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
# Rule 2e — periodic D floor (4+ hours without D → D)
#
# Design: cortex-memory/research/2026-05-24-stage-d-minimum-guarantee.md
# Impl:   cortex-memory/research/2026-05-24-stage-d-minimum-guarantee-impl-guide.md
# ---------------------------------------------------------------------------


def test_rule_2e_fires_when_4h_elapsed_with_corpus() -> None:
    """4+ hours since last D + corpus exists + cap not exhausted +
    inbox empty + consecutive_b below threshold → SLEEP_D.
    Without this rule, hour 23 with these inputs would default to
    SLEEP_C via Rule 2c (the structural starvation pattern)."""
    snap = _snap(
        hour=23,
        has_recent_research=True,
        hours_since_last_d=10.0,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2e_fires_on_exact_4h_boundary() -> None:
    """4.0 hours is the threshold — boundary fires."""
    snap = _snap(
        hour=1,
        has_recent_research=True,
        hours_since_last_d=4.0,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2e_does_not_fire_below_4h() -> None:
    """<4h since last D → Rule 2e is silent and Rule 2c takes over."""
    snap = _snap(
        hour=0,
        has_recent_research=True,
        hours_since_last_d=2.0,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2e_does_not_fire_without_corpus() -> None:
    """No research corpus → no point spinning D up."""
    snap = _snap(
        hour=23,
        has_recent_research=False,
        hours_since_last_d=10.0,
    )
    # Falls through to Rule 2c → SLEEP_C in the early window.
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2e_does_not_fire_when_cap_exhausted() -> None:
    """Cap respected — D never exceeds the nightly cap."""
    snap = _snap(
        hour=23,
        has_recent_research=True,
        hours_since_last_d=10.0,
        stage_d_cap_exhausted=True,
    )
    # Falls through to Rule 2c → SLEEP_C in the early window.
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


def test_rule_2e_beats_rule_2a_when_guardrails_met() -> None:
    """Rule 2e (4h+ D floor) is evaluated BEFORE Rule 2a (inbox → B)
    as of 2026-05-25. Rationale: cozylobe sensor notes keep the inbox
    perpetually non-empty, which caused a 4-day Stage D drought
    (2026-05-22 → 2026-05-25) when Rule 2a always won. Rule 2e's own
    guardrails (>=4h since last D, has_recent_research, cap not
    exhausted) bound the promotion to at most one D wake per 4-hour
    window, only when fresh material exists, capped per night."""
    snap = _snap(
        hour=23,
        has_inbox_items=True,
        has_recent_research=True,
        hours_since_last_d=10.0,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_D


def test_rule_2a_still_wins_when_rule_2e_guardrails_unmet() -> None:
    """When Rule 2e's guardrails are NOT satisfied (no recent research),
    Rule 2a's inbox-drain semantics are preserved — inbox → SLEEP_B."""
    snap = _snap(
        hour=23,
        has_inbox_items=True,
        has_recent_research=False,
        hours_since_last_d=10.0,
    )
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_B


def test_rule_2b_consecutive_b_loop_still_breaks_before_2e() -> None:
    """Rule 2b (6+ consecutive B → break loop) fires before Rule 2e —
    the loop-breaker preserves its existing semantics."""
    # 6 consecutive B + corpus → Rule 2b returns SLEEP_D regardless
    # of hours_since_last_d. The behavior is the same either way, but
    # placement matters for the no-corpus case:
    snap = _snap(
        hour=23,
        consecutive_b=6,
        has_recent_research=False,
        hours_since_last_d=10.0,
    )
    # Rule 2b without corpus returns C; Rule 2e gated on corpus so
    # it can't fire either. Confirms Rule 2b is reached first and
    # Rule 2e doesn't override its no-corpus branch.
    assert select_phase(snap, _full_cfg()) is Phase.SLEEP_C


# ---------------------------------------------------------------------------
# _hours_since_last_d — helper that drives Rule 2e
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, minute, tzinfo=_dt.timezone.utc)


def test_hours_since_last_d_returns_inf_when_no_file(
    tmp_path: pathlib.Path,
) -> None:
    """No stage-d-pairs files at all → infinity (no D wake exists)."""
    assert _hours_since_last_d(tmp_path, now=_utc(2026, 5, 24, 12)) == float("inf")


def test_hours_since_last_d_returns_inf_when_no_synthesis(
    tmp_path: pathlib.Path,
) -> None:
    """File exists but no record carries a non-empty synthesis →
    no completed D wake → infinity."""
    today = "2026-05-24"
    (tmp_path / f"stage-d-pairs-{today}.jsonl").write_text(
        '{"ts": "2026-05-24T03:00:00Z", "synthesis": ""}\n'
        '{"ts": "2026-05-24T05:00:00Z", "synthesis": null}\n'
    )
    assert _hours_since_last_d(tmp_path, now=_utc(2026, 5, 24, 12)) == float("inf")


def test_hours_since_last_d_computes_delta_for_completed_d_wake(
    tmp_path: pathlib.Path,
) -> None:
    """One completed D wake 6 hours before `now` → 6.0."""
    today = "2026-05-24"
    (tmp_path / f"stage-d-pairs-{today}.jsonl").write_text(
        '{"ts": "2026-05-24T03:00:00Z", "synthesis": "synth note"}\n'
    )
    result = _hours_since_last_d(tmp_path, now=_utc(2026, 5, 24, 9))
    assert result == pytest.approx(6.0)


def test_hours_since_last_d_picks_most_recent_synthesis(
    tmp_path: pathlib.Path,
) -> None:
    """Multiple synthesis records → use the latest timestamp."""
    today = "2026-05-24"
    (tmp_path / f"stage-d-pairs-{today}.jsonl").write_text(
        '{"ts": "2026-05-24T01:00:00Z", "synthesis": "first"}\n'
        '{"ts": "2026-05-24T07:00:00Z", "synthesis": "latest"}\n'
        '{"ts": "2026-05-24T04:00:00Z", "synthesis": "middle"}\n'
    )
    result = _hours_since_last_d(tmp_path, now=_utc(2026, 5, 24, 10))
    assert result == pytest.approx(3.0)


def test_hours_since_last_d_scans_yesterday_file(
    tmp_path: pathlib.Path,
) -> None:
    """Yesterday's pairs file is also scanned so a late-night D wake
    is found when `now` rolls past midnight."""
    yesterday = "2026-05-23"
    (tmp_path / f"stage-d-pairs-{yesterday}.jsonl").write_text(
        '{"ts": "2026-05-23T23:00:00Z", "synthesis": "late night D"}\n'
    )
    # now = 2026-05-24 02:00 UTC → 3h since the 23:00 UTC synthesis.
    result = _hours_since_last_d(tmp_path, now=_utc(2026, 5, 24, 2))
    assert result == pytest.approx(3.0)


def test_hours_since_last_d_ignores_malformed_lines(
    tmp_path: pathlib.Path,
) -> None:
    """Malformed JSON, missing ts, and blank lines all skipped without
    crashing. The one valid synthesis record drives the result."""
    today = "2026-05-24"
    (tmp_path / f"stage-d-pairs-{today}.jsonl").write_text(
        "\n"
        "{not valid json\n"
        '{"synthesis": "no ts field"}\n'
        '{"ts": "not-a-timestamp", "synthesis": "bad ts"}\n'
        '{"ts": "2026-05-24T06:00:00Z", "synthesis": "valid"}\n'
        "   \n"
    )
    result = _hours_since_last_d(tmp_path, now=_utc(2026, 5, 24, 10))
    assert result == pytest.approx(4.0)


def test_hours_since_last_d_handles_local_time_now(
    tmp_path: pathlib.Path,
) -> None:
    """UTC/local off-by-N-hours protection: when `now` is tz-aware
    local (e.g. EDT, UTC-4), the helper normalizes both ends to UTC
    so the delta is correct."""
    today = "2026-05-24"
    (tmp_path / f"stage-d-pairs-{today}.jsonl").write_text(
        '{"ts": "2026-05-24T03:00:00Z", "synthesis": "synth"}\n'
    )
    # 2026-05-24 05:00 EDT == 2026-05-24 09:00 UTC. 6h elapsed since
    # the 03:00 UTC synthesis — NOT 2h (the bug if we forget to
    # normalize timezones).
    edt = _dt.timezone(_dt.timedelta(hours=-4))
    now_local = _dt.datetime(2026, 5, 24, 5, 0, tzinfo=edt)
    result = _hours_since_last_d(tmp_path, now=now_local)
    assert result == pytest.approx(6.0)


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


def test_build_vault_snapshot_counts_consecutive_b_from_wake_files(
    tmp_path: pathlib.Path,
) -> None:
    """The consecutive_b/consecutive_null_c counters now derive from
    ``inner/thoughts/`` wake-file frontmatter rather than from
    counter-files no code path actually wrote. Regression: the prior
    ``_read_counter`` implementation always returned 0 because the
    counter files never existed."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    thoughts = tmp_path / "inner" / "thoughts" / "2026-05-07"
    thoughts.mkdir(parents=True)
    # Three consecutive Stage B wakes, all did_work:false → streak 3.
    import os as _os

    for i, hhmmss in enumerate(("230000", "230500", "231000")):
        wake = thoughts / f"{hhmmss}-wake.md"
        wake.write_text("---\nmode: sleep\nstage: B\ndid_work: false\n---\n")
        # Force ascending mtimes so newest-first walking works.
        ts = _now().timestamp() - 3600 + i  # within the 24h window
        _os.utime(wake, (ts, ts))
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.consecutive_b == 3


def test_build_vault_snapshot_consecutive_b_breaks_on_did_work_true(
    tmp_path: pathlib.Path,
) -> None:
    """A Stage B wake with did_work:true breaks the null-pass streak —
    the model is doing real work, so we don't want to escalate."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    thoughts = tmp_path / "inner" / "thoughts" / "2026-05-07"
    thoughts.mkdir(parents=True)
    import os as _os

    # Newest first: the most recent wake had did_work:true → streak resets.
    entries = (
        ("230000", "false"),
        ("230500", "false"),
        ("231000", "true"),
    )
    for i, (hhmmss, did_work) in enumerate(entries):
        wake = thoughts / f"{hhmmss}-wake.md"
        wake.write_text(f"---\nmode: sleep\nstage: B\ndid_work: {did_work}\n---\n")
        ts = _now().timestamp() - 3600 + i  # within the 24h window
        _os.utime(wake, (ts, ts))
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.consecutive_b == 0


def test_build_vault_snapshot_consecutive_b_breaks_on_different_stage(
    tmp_path: pathlib.Path,
) -> None:
    """A non-B stage frontmatter (e.g. Stage C) breaks the streak."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    thoughts = tmp_path / "inner" / "thoughts" / "2026-05-07"
    thoughts.mkdir(parents=True)
    import os as _os

    # Newest most recent: stage C → streak should be 0 (most recent isn't B).
    for i, (hhmmss, stage) in enumerate(
        (("230000", "B"), ("230500", "B"), ("231000", "C"))
    ):
        wake = thoughts / f"{hhmmss}-wake.md"
        wake.write_text(f"---\nmode: sleep\nstage: {stage}\ndid_work: false\n---\n")
        ts = _now().timestamp() - 3600 + i  # within the 24h window
        _os.utime(wake, (ts, ts))
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.consecutive_b == 0


def test_build_vault_snapshot_handles_missing_thoughts_dir(
    tmp_path: pathlib.Path,
) -> None:
    """No thoughts dir at all → counters return 0 cleanly."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    snap = build_vault_snapshot(tmp_path, now=_now(), state_dir=state_dir)
    assert snap.consecutive_b == 0
    assert snap.consecutive_null_c == 0


def test_build_vault_snapshot_detects_stage_d_cap_exhaustion(
    tmp_path: pathlib.Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    today = _now().date().isoformat()
    pairs = state_dir / f"stage-d-pairs-{today}.jsonl"
    pairs.write_text(
        "\n".join(f'{{"ts": "x", "synthesis": "s{i}"}}' for i in range(STAGE_D_NIGHTLY_CAP)) + "\n"
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
    out = loader.compose(
        Phase.ACTIVE,
        timestamp_header="Current local time: 2026-05-07 14:00 EDT (Thursday)",
    )
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


def test_loader_per_issue_phases_skip_prelude() -> None:
    """Per-issue phases (#163) bypass the wake-mode prelude. The
    prelude's "no writes outside ~/alice-mind/" constitutional
    boundary would prevent BUILD from opening PRs; per-issue
    fragments carry their own framing.
    """
    loader = PromptFragmentLoader()
    for phase in (Phase.PER_ISSUE_DESIGN, Phase.PER_ISSUE_BUILD):
        out = loader.compose(
            phase,
            timestamp_header="ts",
            injected_content="<<ENTRY CONTEXT>>",
        )
        # No wake prelude.
        assert "Thinking Alice — wake" not in out
        assert "Step 1 — write the wake file" not in out
        # Injected content lands inline.
        assert "<<ENTRY CONTEXT>>" in out
        # Phase-specific Step 0 framing is present.
        assert "Step 0" in out


def test_loader_per_issue_design_fragment_emits_design_ready_contract() -> None:
    """The DESIGN fragment must instruct the agent to emit the
    ``[SM] design-ready`` comment — without it, Speaking has no
    review trigger and the pipeline stalls."""
    loader = PromptFragmentLoader()
    out = loader.load_phase(Phase.PER_ISSUE_DESIGN)
    assert "[SM] design-ready" in out
    assert "[SM] design-revise" in out


def test_loader_per_issue_build_fragment_emits_draft_pr_contract() -> None:
    """The BUILD fragment must instruct the agent to open a draft PR
    (not self-merge from build phase — that's sub-issue 6's
    reviewer's job)."""
    loader = PromptFragmentLoader()
    out = loader.load_phase(Phase.PER_ISSUE_BUILD)
    assert "draft" in out.lower()
    assert "PR" in out
    # No --no-verify, no force-push — kernel constraints.
    assert "--no-verify" in out


# ---------------------------------------------------------------------------
# detect_commission_notes
# ---------------------------------------------------------------------------


def test_detect_commission_notes_frontmatter(tmp_path: pathlib.Path) -> None:
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    (notes / "regular.md").write_text("---\nkind: chat\n---\n")
    target = notes / "needs-design.md"
    target.write_text("---\ntask_type: design-commission\nslug: foo\n---\n\nbody\n")
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


def test_record_conflict_deferral_increments_count(
    tmp_path: pathlib.Path,
) -> None:
    """First defer writes ``defer_count: 1``; status stays ``open``."""
    conflict = tmp_path / "c.md"
    conflict.write_text("---\nstatus: open\n---\n\nbody\n")

    new_count, marked_stale = record_conflict_deferral(conflict)

    assert (new_count, marked_stale) == (1, False)
    text = conflict.read_text()
    assert "defer_count: 1" in text
    assert "status: open" in text


def test_record_conflict_deferral_bumps_existing_count(
    tmp_path: pathlib.Path,
) -> None:
    """Existing ``defer_count`` is read, incremented, and rewritten."""
    conflict = tmp_path / "c.md"
    conflict.write_text("---\nstatus: open\ndefer_count: 2\n---\n\nbody\n")

    new_count, marked_stale = record_conflict_deferral(conflict)

    assert new_count == 3
    assert marked_stale is False
    assert "defer_count: 3" in conflict.read_text()


def test_record_conflict_deferral_flips_to_stale_at_threshold(
    tmp_path: pathlib.Path,
) -> None:
    """Reaching the threshold flips ``status`` to ``stale``."""
    conflict = tmp_path / "c.md"
    prev = CONFLICT_DEFER_THRESHOLD - 1
    conflict.write_text(
        f"---\nstatus: open\ndefer_count: {prev}\n---\n\nbody\n"
    )

    new_count, marked_stale = record_conflict_deferral(conflict)

    assert new_count == CONFLICT_DEFER_THRESHOLD
    assert marked_stale is True
    text = conflict.read_text()
    assert f"defer_count: {CONFLICT_DEFER_THRESHOLD}" in text
    assert "status: stale" in text
    assert "status: open" not in text


def test_record_conflict_deferral_threshold_drops_note_from_queue(
    tmp_path: pathlib.Path,
) -> None:
    """After ``CONFLICT_DEFER_THRESHOLD`` deferrals the note no longer
    shows up in :func:`detect_conflict_notes` — exactly the bail-out
    behaviour issue #203 needs (the wake stops preempting on it).
    """
    conflicts = tmp_path / "conflicts"
    conflicts.mkdir()
    note = conflicts / "stuck.md"
    note.write_text("---\nstatus: open\n---\n\nbody\n")

    marked_stale = False
    for _ in range(CONFLICT_DEFER_THRESHOLD):
        _, marked_stale = record_conflict_deferral(note)
    assert marked_stale is True

    assert detect_conflict_notes(tmp_path) == []


def test_record_conflict_deferral_handles_missing_frontmatter(
    tmp_path: pathlib.Path,
) -> None:
    """A note with no frontmatter still gets a counter; subsequent
    detection treats absent ``status`` as open (existing semantics)
    while ``defer_count`` accumulates toward the threshold.
    """
    conflict = tmp_path / "c.md"
    conflict.write_text("just a body, no frontmatter\n")

    new_count, marked_stale = record_conflict_deferral(conflict)

    assert (new_count, marked_stale) == (1, False)
    text = conflict.read_text()
    assert "defer_count: 1" in text
    assert "just a body" in text


def test_record_conflict_deferral_custom_threshold(
    tmp_path: pathlib.Path,
) -> None:
    """``threshold`` is configurable per call (tests / future tuning)."""
    conflict = tmp_path / "c.md"
    conflict.write_text("---\nstatus: open\n---\n\nbody\n")

    _, marked_stale = record_conflict_deferral(conflict, threshold=2)
    assert marked_stale is False
    _, marked_stale = record_conflict_deferral(conflict, threshold=2)
    assert marked_stale is True
    assert "status: stale" in conflict.read_text()


def test_select_phase_does_not_dispatch_to_conflict_resolution() -> None:
    """``Phase.CONFLICT_RESOLUTION`` is task-detected (mirrors
    ``DESIGN_COMMISSION``), not cadence-dispatched. The selector
    must never return it from any vault snapshot — the preempt
    lives in ``wake.py``.
    """
    # Sweep representative snapshots across the day; none should
    # produce CONFLICT_RESOLUTION.
    snaps = [_snap(hour=h) for h in (0, 1, 4, 7, 12, 16, 22, 23)] + [
        _snap(hour=2, has_inbox_items=True),
        _snap(hour=4, has_recent_research=True),
        _snap(hour=1, consecutive_b=10, has_recent_research=True),
    ]
    for snap in snaps:
        out = select_phase(snap)
        assert out is not Phase.CONFLICT_RESOLUTION


# ---------------------------------------------------------------------------
# `cortex-memory/unresolved.md` probes — has_broken_links, has_orphan_stubs.
# ---------------------------------------------------------------------------
#
# Regression: prior implementations returned True whenever the file had
# any text, but the file always carries frontmatter + tl;dr + usage prose,
# so has_orphan_stubs fired every wake and pinned Rule 2a to SLEEP_B even
# when the live ## Open section was marked empty. Scope inspection to the
# Open section.


def _write_unresolved(mind: pathlib.Path, body: str) -> None:
    cm = mind / "cortex-memory"
    cm.mkdir(parents=True, exist_ok=True)
    (cm / "unresolved.md").write_text(body)


def test_has_orphan_stubs_returns_false_when_open_section_is_placeholder(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_orphan_stubs

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\ntags: [backlog]\n---\n\n"
        "# unresolved\n\n"
        "> **tl;dr** Backlog of dangling wikilinks; currently empty.\n\n"
        "Wikilinks referenced somewhere in the vault that don't yet have a note.\n\n"
        "## Open\n\n"
        "*(empty — all previously-listed unresolved links now have notes)*\n",
    )
    assert _has_orphan_stubs(tmp_path) is False


def test_has_orphan_stubs_returns_false_when_file_has_no_open_section(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_orphan_stubs

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\n---\n\n"
        "# unresolved\n\n"
        "Frontmatter and tl;dr only. No Open H2.\n",
    )
    assert _has_orphan_stubs(tmp_path) is False


def test_has_orphan_stubs_returns_true_when_open_section_has_entry(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_orphan_stubs

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\n---\n\n"
        "## Open\n\n"
        "- [[ghost-concept]] — referenced from research/foo.md, no note yet\n",
    )
    assert _has_orphan_stubs(tmp_path) is True


def test_has_orphan_stubs_returns_false_when_file_missing(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_orphan_stubs

    (tmp_path / "cortex-memory").mkdir()
    assert _has_orphan_stubs(tmp_path) is False


def test_has_broken_links_ignores_placeholder_open_section(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_broken_links

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\n---\n\n"
        "## Open\n\n"
        "*(empty — all previously-listed unresolved links now have notes)*\n",
    )
    assert _has_broken_links(tmp_path) is False


def test_has_broken_links_ignores_wikilinks_outside_open_section(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_broken_links

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\n---\n\n"
        "# unresolved\n\n"
        "> **tl;dr** Backlog. Use [[ops-document]] to fill entries.\n\n"
        "Body text mentions [[example-concept]] for instructional reasons.\n\n"
        "## Open\n\n"
        "*(empty)*\n",
    )
    assert _has_broken_links(tmp_path) is False


def test_has_broken_links_returns_true_for_real_open_entry(
    tmp_path: pathlib.Path,
) -> None:
    from alice_thinking.phase import _has_broken_links

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\n---\n\n"
        "## Open\n\n"
        "- [[broken-target]] referenced from research/bar.md\n",
    )
    assert _has_broken_links(tmp_path) is True


def test_has_broken_links_stops_at_next_h2_header(
    tmp_path: pathlib.Path,
) -> None:
    """An H2 after Open closes the section. Wikilinks below should
    not count toward broken-link detection."""
    from alice_thinking.phase import _has_broken_links

    _write_unresolved(
        tmp_path,
        "---\ntitle: unresolved\n---\n\n"
        "## Open\n\n"
        "*(empty)*\n\n"
        "## Closed\n\n"
        "- [[resolved-link]] resolved 2026-05-01\n",
    )
    assert _has_broken_links(tmp_path) is False
