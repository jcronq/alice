"""Plan 03 Phase 3: vault state snapshot.

Tests the parsing surface (frontmatter robustness), the four
state fields the selector + Phase-4 sleep sub-stage logic care
about, and the missing-mind-dir fallback (snapshot must not raise
on a partial scaffold).
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from alice_thinking.vault_state import (
    STAGE_D_NIGHTLY_CAP,
    VaultState,
    snapshot,
)


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 4, 30, 14, 0)


def _write_wake(
    mind: pathlib.Path,
    *,
    day: str,
    hhmmss: str,
    stage: str,
    did_work: str,
    mtime: float | None = None,
) -> pathlib.Path:
    d = mind / "inner" / "thoughts" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{hhmmss}-wake.md"
    p.write_text(
        "---\n"
        f"mode: sleep\n"
        f"stage: {stage}\n"
        f"did_work: {did_work}\n"
        "---\n\n"
        "body\n"
    )
    if mtime is not None:
        import os

        os.utime(p, (mtime, mtime))
    return p


def test_snapshot_handles_missing_mind_dir_gracefully(
    tmp_path: pathlib.Path,
) -> None:
    """No ``inner/`` subtree → snapshot returns sensible defaults
    instead of raising. The wake fires in fresh / partial scaffolds."""
    out = snapshot(tmp_path, now=_now())
    assert isinstance(out, VaultState)
    assert out.has_pending_inbox is False
    assert out.has_recent_research_corpus is False
    assert out.consecutive_b_wakes == 0
    assert out.consecutive_null_c_wakes == 0
    assert out.stage_d_cap_exhausted is False


def test_snapshot_detects_pending_inbox(tmp_path: pathlib.Path) -> None:
    notes = tmp_path / "inner" / "notes"
    notes.mkdir(parents=True)
    (notes / "pending.md").write_text("# fresh thought")
    out = snapshot(tmp_path, now=_now())
    assert out.has_pending_inbox is True
    # is_stable is the inverse — used by the SleepMode selector.
    assert out.is_stable is False


def test_snapshot_detects_recent_research_corpus(
    tmp_path: pathlib.Path,
) -> None:
    research = tmp_path / "cortex-memory" / "research"
    research.mkdir(parents=True)
    # Two recent files → eligible.
    (research / "a.md").write_text("a")
    (research / "b.md").write_text("b")
    out = snapshot(tmp_path, now=_now())
    assert out.has_recent_research_corpus is True
    assert out.research_corpus_age_days is not None


def test_snapshot_corpus_requires_two_recent(tmp_path: pathlib.Path) -> None:
    """One recent file isn't enough — Stage D needs a pair."""
    research = tmp_path / "cortex-memory" / "research"
    research.mkdir(parents=True)
    (research / "a.md").write_text("a")
    out = snapshot(tmp_path, now=_now())
    assert out.has_recent_research_corpus is False


def test_snapshot_counts_consecutive_b_wakes(tmp_path: pathlib.Path) -> None:
    """Stage B with did_work=false in the last 3h, walking newest →
    oldest, counts up to the first non-matching wake."""
    now = _now()
    base = now.timestamp() - 7200  # 2h ago
    _write_wake(tmp_path, day="2026-04-30", hhmmss="120001", stage="B", did_work="false", mtime=base + 0)
    _write_wake(tmp_path, day="2026-04-30", hhmmss="120002", stage="B", did_work="false", mtime=base + 60)
    _write_wake(tmp_path, day="2026-04-30", hhmmss="120003", stage="B", did_work="false", mtime=base + 120)
    out = snapshot(tmp_path, now=now)
    assert out.consecutive_b_wakes == 3


def test_snapshot_b_streak_breaks_on_did_work_true(
    tmp_path: pathlib.Path,
) -> None:
    """A productive Stage B wake breaks the consecutive-null streak."""
    now = _now()
    base = now.timestamp() - 5400  # 1.5h ago
    _write_wake(tmp_path, day="2026-04-30", hhmmss="000001", stage="B", did_work="true", mtime=base)
    _write_wake(tmp_path, day="2026-04-30", hhmmss="000002", stage="B", did_work="false", mtime=base + 60)
    _write_wake(tmp_path, day="2026-04-30", hhmmss="000003", stage="B", did_work="false", mtime=base + 120)
    out = snapshot(tmp_path, now=now)
    # Walking newest → oldest, the first 2 are false-streak, then
    # the third (did_work=true) breaks the streak.
    assert out.consecutive_b_wakes == 2


def test_snapshot_did_work_parser_robust_to_commentary(
    tmp_path: pathlib.Path,
) -> None:
    """``did_work: false — closing clean.`` should still count as
    falsey (matches the convention seen in real wake files)."""
    now = _now()
    base = now.timestamp() - 60
    p = _write_wake(tmp_path, day="2026-04-30", hhmmss="170000", stage="B", did_work="false", mtime=base)
    # Rewrite to add commentary.
    p.write_text(
        "---\nmode: sleep\nstage: B\ndid_work: false — closing clean.\n---\n\nbody\n"
    )
    import os

    os.utime(p, (base, base))
    out = snapshot(tmp_path, now=now)
    assert out.consecutive_b_wakes == 1


def test_snapshot_counts_consecutive_null_c_wakes(
    tmp_path: pathlib.Path,
) -> None:
    now = _now()
    base = now.timestamp() - 600
    _write_wake(tmp_path, day="2026-04-30", hhmmss="010000", stage="C", did_work="false", mtime=base + 0)
    _write_wake(tmp_path, day="2026-04-30", hhmmss="010001", stage="C", did_work="false", mtime=base + 60)
    out = snapshot(tmp_path, now=now)
    assert out.consecutive_null_c_wakes == 2


def test_snapshot_detects_stage_d_cap_exhausted(
    tmp_path: pathlib.Path,
) -> None:
    """N entries in stage-d-pairs-YYYY-MM-DD.jsonl trips the cap."""
    state = tmp_path / "inner" / "state"
    state.mkdir(parents=True)
    today = _now().date().isoformat()
    pairs = state / f"stage-d-pairs-{today}.jsonl"
    pairs.write_text(
        "\n".join(f'{{"i": {i}}}' for i in range(STAGE_D_NIGHTLY_CAP)) + "\n"
    )
    out = snapshot(tmp_path, now=_now())
    assert out.stage_d_cap_exhausted is True


def test_snapshot_stage_d_cap_not_yet(tmp_path: pathlib.Path) -> None:
    """Below cap → not exhausted."""
    state = tmp_path / "inner" / "state"
    state.mkdir(parents=True)
    today = _now().date().isoformat()
    (state / f"stage-d-pairs-{today}.jsonl").write_text('{"i": 0}\n')
    out = snapshot(tmp_path, now=_now())
    assert out.stage_d_cap_exhausted is False


def test_snapshot_recency_window_excludes_old_thoughts(
    tmp_path: pathlib.Path,
) -> None:
    """A wake file 5h old is outside the default 3h window — doesn't
    count toward the consecutive streak."""
    now = _now()
    old = now.timestamp() - 5 * 3600  # 5h ago
    _write_wake(tmp_path, day="2026-04-30", hhmmss="090000", stage="B", did_work="false", mtime=old)
    out = snapshot(tmp_path, now=now)
    assert out.consecutive_b_wakes == 0
