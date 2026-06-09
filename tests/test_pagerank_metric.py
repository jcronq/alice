"""Tests for ``metrics.pagerank_metric``.

The metric is built against ``cortex-index.db`` in production. These
tests use a small synthetic in-memory schema fixture instead — the
production vault would be a noisy, drifting target and would tie test
correctness to the current vault topology.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from metrics.pagerank_metric import (
    compute_pagerank,
    compute_weighted_sum,
    linkedness_tier,
    tier_counts,
)


# Minimal subset of the cortex-index schema — just the columns the
# metric reads. The real DDL lives in indexer/build_index.py; keeping
# this slim avoids coupling the test to unrelated schema churn.
_SCHEMA = """
CREATE TABLE notes (
    slug TEXT PRIMARY KEY
);
CREATE TABLE links (
    source_slug TEXT NOT NULL,
    target_slug TEXT NOT NULL,
    is_structural INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0
);
"""


def _build_fixture_db(
    tmp_path: Path,
    nodes: list[str],
    edges: list[tuple[str, str]],
    *,
    structural: bool = True,
    resolved: int = 1,
) -> Path:
    """Materialise a synthetic ``cortex-index.db``-shaped sqlite file.

    All edges are written with ``is_structural=1`` and ``resolved=1`` by
    default — that matches the live vault's invariant (every structural
    link in the live DB has ``resolved=1``). The metric must NOT filter
    on ``resolved=0`` or it returns an empty graph; one of the tests
    pins that contract.
    """
    db_path = tmp_path / "cortex-index.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO notes(slug) VALUES (?)", [(n,) for n in nodes])
        conn.executemany(
            "INSERT INTO links(source_slug, target_slug, is_structural, resolved) "
            "VALUES (?, ?, ?, ?)",
            [(s, t, 1 if structural else 0, resolved) for s, t in edges],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _hub_and_isolate_db(tmp_path: Path) -> Path:
    """Graph: hub ← {a,b,c,d}, hub → spoke; iso has no edges.

    ``hub`` gets four inbound links so its PageRank is high. ``spoke``
    is linked only to ``hub`` (a high-PR node), so its weighted sum
    should beat a peripheral node like ``a``. ``iso`` has no links at
    all, so it should be 0.0 — the binary-tier ``"isolated"`` case.
    """
    nodes = ["hub", "spoke", "a", "b", "c", "d", "iso"]
    edges = [
        ("a", "hub"),
        ("b", "hub"),
        ("c", "hub"),
        ("d", "hub"),
        ("hub", "spoke"),
    ]
    return _build_fixture_db(tmp_path, nodes, edges)


# ---------------------------------------------------------------------------
# compute_pagerank


def test_compute_pagerank_sums_to_one(tmp_path: Path) -> None:
    """PageRank values across all nodes sum to ~1.0.

    This is the textbook normalisation invariant — if it breaks, the
    metric's downstream weighted-sum formula breaks too.
    """
    db = _hub_and_isolate_db(tmp_path)
    pr = compute_pagerank(db)
    assert set(pr.keys()) == {"hub", "spoke", "a", "b", "c", "d", "iso"}
    assert sum(pr.values()) == pytest.approx(1.0, rel=1e-6)


def test_compute_pagerank_hub_outranks_periphery(tmp_path: Path) -> None:
    """The hub with 4 inbound links should outrank a peripheral node."""
    db = _hub_and_isolate_db(tmp_path)
    pr = compute_pagerank(db)
    assert pr["hub"] > pr["a"]
    assert pr["hub"] > pr["iso"]


# ---------------------------------------------------------------------------
# compute_weighted_sum


def test_weighted_sum_linked_to_hub_beats_isolated(tmp_path: Path) -> None:
    """A note linked to a high-PR hub gets a higher weighted sum than an
    isolated note — the headline behaviour the metric was designed for.

    The spec's claim ([[2026-06-08-pagerank-weighted-sum-metric-design]],
    §Interpretation): "A note with high pr_ws is well-connected
    (surrounded by other well-connected notes). A note with pr_ws = 0
    is structurally isolated (no structural links)."
    """
    db = _hub_and_isolate_db(tmp_path)
    pr_ws = compute_weighted_sum(db)

    # ``spoke`` is linked only to the high-PR hub; ``iso`` has no edges.
    assert pr_ws["spoke"] > 0.0
    assert pr_ws["iso"] == 0.0
    assert pr_ws["spoke"] > pr_ws["iso"]
    # ``hub`` aggregates contributions from four leaf predecessors, all
    # of which themselves only point at ``hub`` — its score must clear
    # an isolated peripheral.
    assert pr_ws["hub"] > pr_ws["iso"]


def test_weighted_sum_many_high_rank_neighbors_beats_few(tmp_path: Path) -> None:
    """A note connected to many high-PR neighbors outranks a sparsely
    connected one. Direct read of the design-note claim.

    Fixture: ``well_connected`` has four predecessors, each of which is
    itself the target of two other notes (so they have non-trivial PR).
    ``sparse`` has a single predecessor with no inbound mass of its own.
    """
    # Layer 1: low-rank leaves a1..a4 each point at one of b1..b4.
    # Layer 2: b1..b4 then all point at well_connected.
    # sparse is reached only by a single leaf c1 (with no further mass).
    nodes = [
        "well_connected",
        "sparse",
        "b1", "b2", "b3", "b4",
        "a1a", "a1b", "a2a", "a2b", "a3a", "a3b", "a4a", "a4b",
        "c1",
    ]
    edges = (
        [("a1a", "b1"), ("a1b", "b1"),
         ("a2a", "b2"), ("a2b", "b2"),
         ("a3a", "b3"), ("a3b", "b3"),
         ("a4a", "b4"), ("a4b", "b4")]
        + [("b1", "well_connected"), ("b2", "well_connected"),
           ("b3", "well_connected"), ("b4", "well_connected")]
        + [("c1", "sparse")]
    )
    db = _build_fixture_db(tmp_path, nodes, edges)
    pr_ws = compute_weighted_sum(db)
    assert pr_ws["well_connected"] > pr_ws["sparse"]
    assert pr_ws["sparse"] > 0.0  # still linked (binary tier)


def test_weighted_sum_ignores_resolved_zero_paranoia(tmp_path: Path) -> None:
    """Regression guard for spec correction #1.

    The implementation spec said ``WHERE is_structural = 1 AND
    resolved = 0`` — the live vault has every structural link at
    ``resolved=1``, so that filter would produce an empty graph and
    every score would be 0.0. Building the fixture with the live
    invariant (``resolved=1``) and asserting non-empty output locks the
    correct behaviour in.
    """
    db = _hub_and_isolate_db(tmp_path)  # resolved=1 by default
    pr_ws = compute_weighted_sum(db)
    # At least one node must have a positive score; if the resolved=0
    # filter ever sneaks back in, every score collapses to 0.0.
    assert any(score > 0.0 for score in pr_ws.values())


def test_weighted_sum_empty_db(tmp_path: Path) -> None:
    """An empty ``notes`` table should return an empty dict (no crash)."""
    db = _build_fixture_db(tmp_path, nodes=[], edges=[])
    assert compute_weighted_sum(db) == {}
    assert compute_pagerank(db) == {}


# ---------------------------------------------------------------------------
# linkedness_tier (binary)


def test_tier_isolated_for_zero() -> None:
    assert linkedness_tier(0.0) == "isolated"


def test_tier_linked_for_positive() -> None:
    assert linkedness_tier(1e-9) == "linked"
    assert linkedness_tier(0.001) == "linked"
    assert linkedness_tier(0.5) == "linked"


def test_tier_counts_summary(tmp_path: Path) -> None:
    """``tier_counts`` returns the dict shape the vault_health event consumes."""
    db = _hub_and_isolate_db(tmp_path)
    pr_ws = compute_weighted_sum(db)
    counts = tier_counts(pr_ws)
    assert set(counts.keys()) == {"isolated", "linked"}
    # iso is the only fully-isolated node in this fixture.
    assert counts["isolated"] == 1
    assert counts["linked"] == len(pr_ws) - 1
