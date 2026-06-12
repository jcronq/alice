"""Multi-signal decay scoring for the memory worker.

Replaces the single-axis ``access_count`` model (decay = last_accessed
older than 90 days AND access_count <= 1) with a weighted combination
of four orthogonal signals:

1. **S_access** — behavioral access (log-scale access_count)
2. **S_struct** — structural connectivity (cross-directory inbound links)
3. **S_organic** — organic connectivity (all inbound wikilinks, regardless of directory)
4. **S_correction** — correction cascade reinforcement (0.0 until cascade merges)

The combined formula:

    D_weighted = w1·(1 - S_access) + w2·(1 - S_struct) + w3·(1 - S_organic) + w4·age_factor
    D(n)       = D_weighted × (1 - S_correction)

with weights ``w1=0.30, w2=0.30, w3=0.25, w4=0.15`` and decay
threshold ``0.25``.

Design specification:
``cortex-memory/research/2026-06-12-multi-signal-decay-scoring-design.md``

Backward compatibility: the score range is [0.0, 1.0] with the same
0.25 threshold. Notes with high organic connectivity but low structural
in-degree will see reduced scores (fewer false positives).

S_correction integration: defaults to 0.0 (no boost) until the
correction cascade pipeline is merged from the
``thinking-correction-cascade-wip`` branch. When available, the
``load_correction_counts`` function will return non-zero values.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Weight calibration
# ---------------------------------------------------------------------------

W_ACCESS: float = 0.30
W_STRUCT: float = 0.30
W_ORGANIC: float = 0.25
W_AGE: float = 0.15

# Decay threshold (unchanged from Phase A).
DECAY_THRESHOLD: float = 0.25

# Structural in-degree normalization denominator.
# A note with 10+ structural inbound links scores 1.0.
STRUCT_DENOM: float = 10.0

# Organic all-links degree normalization denominator.
# A note with 50+ all inbound links scores 1.0.
ORGANIC_DENOM: float = 50.0

# Age decay half-life in days.
AGE_HALF_LIFE: float = 7.0

# Correction boost (binary: 0.0 or 0.5).
CORRECTION_BOOST: float = 0.5

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DecayScore:
    """Decay score and component signals for a single note."""

    decay_score: float
    s_access: float
    s_struct: float
    s_organic: float
    s_correction: float
    age_factor: float
    is_decayed: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_decayed = self.decay_score >= DECAY_THRESHOLD


@dataclass
class ScoringContext:
    """Precomputed data shared across all score computations.

    Built once per call site (e.g. from ``_iter_decayed_notes``) so that
    per-note scoring is pure arithmetic with no I/O.
    """

    db_path: Path
    max_access: float = 0.0
    struct_in_degrees: dict[str, int] = field(default_factory=dict)
    organic_in_degrees: dict[str, int] = field(default_factory=dict)
    access_counts: dict[str, int] = field(default_factory=dict)
    correction_counts: dict[str, int] = field(default_factory=dict)
    all_notes: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------


def build_context(db_path: Path) -> ScoringContext:
    """Query the index DB once and return a precomputed scoring context.

    This is the only I/O-bound call. Every subsequent :func:`score_note`
    call is pure arithmetic over the in-memory dicts.
    """
    ctx = ScoringContext(db_path=db_path)

    if not db_path.exists():
        return ctx

    conn = sqlite3.connect(str(db_path))
    try:
        # --- access counts ---
        rows = conn.execute(
            "SELECT slug, access_count FROM note_metrics"
        ).fetchall()
        for slug, ac in rows:
            ac_int = int(ac) if ac is not None else 0
            ctx.access_counts[slug] = ac_int
            if ac_int > ctx.max_access:
                ctx.max_access = float(ac_int)

        # --- structural in-degrees ---
        struct_rows = conn.execute(
            "SELECT target_slug, COUNT(*) AS cnt "
            "FROM links WHERE is_structural = 1 "
            "GROUP BY target_slug"
        ).fetchall()
        for slug, cnt in struct_rows:
            ctx.struct_in_degrees[slug] = cnt

        # --- organic in-degrees ---
        organic_rows = conn.execute(
            "SELECT target_slug, COUNT(*) AS cnt "
            "FROM links WHERE is_structural = 0 "
            "GROUP BY target_slug"
        ).fetchall()
        for slug, cnt in organic_rows:
            ctx.organic_in_degrees[slug] = cnt

        # --- all notes ---
        notes = conn.execute("SELECT slug FROM notes").fetchall()
        ctx.all_notes = {row[0] for row in notes}
    finally:
        conn.close()

    return ctx


# ---------------------------------------------------------------------------
# Per-note scoring
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> Optional[date]:
    """Parse an ISO-date string (YYYY-MM-DD) or return None."""
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except (ValueError, TypeError):
        return None


def score_note(
    ctx: ScoringContext,
    slug: str,
    access_count: int,
    created: Optional[str],
) -> DecayScore:
    """Compute the full multi-signal decay score for one note.

    Parameters
    ----------
    ctx:
        Precomputed context from :func:`build_context`.
    slug:
        Note slug (used to look up in-degree maps).
    access_count:
        Note's ``access_count`` frontmatter value.
    created:
        Note's ``created`` frontmatter value (ISO date string).

    Returns
    -------
    DecayScore with all component signals and the final ``is_decayed`` flag.
    """
    # --- S_access: log-scale behavioral access ---
    if ctx.max_access > 0:
        s_access = math.log2(1 + access_count) / math.log2(1 + ctx.max_access)
    else:
        s_access = 0.0
    s_access = min(1.0, s_access)

    # --- S_struct: structural connectivity ---
    struct_deg = ctx.struct_in_degrees.get(slug, 0)
    s_struct = min(1.0, struct_deg / STRUCT_DENOM)

    # --- S_organic: all-links degree ---
    organic_deg = ctx.organic_in_degrees.get(slug, 0)
    s_organic = min(1.0, organic_deg / ORGANIC_DENOM)

    # --- S_correction: correction cascade (stub — returns 0.0) ---
    correction_count = ctx.correction_counts.get(slug, 0)
    s_correction = CORRECTION_BOOST if correction_count >= 1 else 0.0

    # --- age_factor: exponential decay since creation ---
    created_date = _parse_date(created) if created else None
    if created_date is not None:
        age_days = max(0, (date.today() - created_date).days)
        age_factor = math.pow(0.5, age_days / AGE_HALF_LIFE)
    else:
        age_factor = 1.0  # unknown age → no age benefit

    # --- D_weighted ---
    d_weighted = (
        W_ACCESS * (1.0 - s_access)
        + W_STRUCT * (1.0 - s_struct)
        + W_ORGANIC * (1.0 - s_organic)
        + W_AGE * age_factor
    )

    # --- D(n): final score with correction multiplier ---
    decay_score = d_weighted * (1.0 - s_correction)

    return DecayScore(
        decay_score=decay_score,
        s_access=s_access,
        s_struct=s_struct,
        s_organic=s_organic,
        s_correction=s_correction,
        age_factor=age_factor,
    )


# ---------------------------------------------------------------------------
# Batch scoring
# ---------------------------------------------------------------------------


def compute_decay_scores(
    ctx: ScoringContext,
    notes: list[tuple[str, int, Optional[str]]],
) -> dict[str, DecayScore]:
    """Compute decay scores for a batch of notes.

    Parameters
    ----------
    notes:
        List of ``(slug, access_count, created)`` tuples.

    Returns
    -------
    ``{slug: DecayScore}`` for every input note.
    """
    return {
        slug: score_note(ctx, slug, ac, created)
        for slug, ac, created in notes
    }


# ---------------------------------------------------------------------------
# Correction cascade integration (stub)
# ---------------------------------------------------------------------------


def load_correction_counts(db_path: Path) -> dict[str, int]:
    """Return ``{slug: correction_count}`` for notes with corrections.

    Currently returns an empty dict — the correction cascade pipeline
    is on the ``thinking-correction-cascade-wip`` branch and not yet
    merged. When merged, this function will query the appropriate
    table / output file.

    The ``note_metrics`` table may eventually store a ``correction_count``
    column. Until then, the correction cascade pipeline writes a
    ``correction_counts.json`` file alongside the index DB.

    See: ``cortex-memory/research/2026-06-12-correction-cascade-pipeline-status``
    """
    # TODO: integrate with correction cascade pipeline output.
    # Expected locations:
    #   - ``note_metrics.correction_count`` (if column added)
    #   - ``<index_db_dir>/correction_counts.json`` (pipeline output)
    return {}


# ---------------------------------------------------------------------------
# Integration helpers for stage_c / stage_d
# ---------------------------------------------------------------------------


def is_decayed(
    ctx: ScoringContext,
    slug: str,
    access_count: int,
    created: Optional[str],
) -> bool:
    """Convenience wrapper: does this note exceed the decay threshold?"""
    return score_note(ctx, slug, access_count, created).is_decayed


def decay_score_for(
    ctx: ScoringContext,
    slug: str,
    access_count: int,
    created: Optional[str],
) -> float:
    """Convenience wrapper: return just the numeric decay score."""
    return score_note(ctx, slug, access_count, created).decay_score
