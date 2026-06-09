"""PageRank-weighted-sum domain-linkedness metric.

The empirically validated vault-agnostic metric for structural
connectedness (Family 2). For each note ``N``::

    pr_ws(N) = Σ_{M ∈ neighbors(N)} pagerank(M) / out_degree(M)

where ``neighbors(N)`` is bidirectional (predecessors ∪ successors) on
the structural wikilink graph from ``cortex-index.db``. AUC=0.969 vs
binary linkability on the current vault (Spearman ρ=0.715 with
in_degree).

Background:

- Design rationale (why PageRank, why weighted sum):
  ``cortex-memory/research/2026-06-08-pagerank-weighted-sum-metric-design.md``
- Implementation spec:
  ``cortex-memory/research/2026-06-08-pagerank-weighted-sum-implementation-spec.md``
- Validation (10-fold CV, AUC=0.969):
  ``cortex-memory/research/2026-06-08-vault-agnostic-metric-validation.md``
- Vault calibration (binary tier decision):
  ``cortex-memory/research/2026-06-08-pagerank-weighted-sum-vault-calibration.md``

Spec corrections applied here (the spec's snippets contradict the
calibration note in three places; the calibration is authoritative):

1. The graph is built from ``WHERE is_structural = 1`` only. The spec
   suggests ``AND resolved = 0`` but every structural link in the live
   vault has ``resolved = 1`` — adding the filter returns zero edges.
2. The schema columns are ``source_slug`` / ``target_slug``, not
   ``source_id`` / ``target_id``. Slugs are the node identifiers
   throughout.
3. The tier classifier is binary (``isolated`` = 0.0, ``linked`` > 0.0)
   per the calibration note: 95.3% of vault notes fall below the spec's
   weak/medium/strong cutoff of 0.001, so finer-grained tiers don't
   buy downstream consumers anything in the current topology. The
   ``LinkednessTier`` Literal can be widened (and ``linkedness_tier``
   can return more values) once the vault is dense enough to support
   it — see TODO below.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

import networkx as nx

# Tier labels exposed to downstream consumers (vault_health event,
# domain inference). Binary today; see TODO inside ``linkedness_tier``.
LinkednessTier = Literal["isolated", "linked"]

# PageRank algorithm parameters. Damping=0.85 is the textbook default
# (Brin & Page 1998); the validation pipeline used the same value.
# tol=1e-6 / max_iter=100 converged in ~50 iters on the live vault.
_PAGERANK_ALPHA = 0.85
_PAGERANK_MAX_ITER = 100
_PAGERANK_TOL = 1.0e-6


def _build_graph(db_path: Path) -> nx.DiGraph:
    """Load structural-link graph from ``cortex-index.db``.

    Nodes are slugs (from the ``notes`` table). Edges come from
    ``links`` rows with ``is_structural=1``; the ``resolved`` column
    is intentionally NOT filtered on — see the module docstring for
    the rationale.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        graph: nx.DiGraph = nx.DiGraph()
        for (slug,) in conn.execute("SELECT slug FROM notes"):
            graph.add_node(slug)
        for source_slug, target_slug in conn.execute(
            "SELECT source_slug, target_slug FROM links WHERE is_structural = 1"
        ):
            # ``add_edge`` will create the target node if it's not in
            # ``notes`` (e.g., a structural link whose target hasn't
            # been ingested yet). That's harmless for PageRank.
            graph.add_edge(source_slug, target_slug)
        return graph
    finally:
        conn.close()


def compute_pagerank(db_path: Path) -> dict[str, float]:
    """Return ``{slug: pagerank}`` over the structural-link graph.

    PageRank is computed with damping=0.85, max_iter=100, tol=1e-6 —
    the same parameters used in the validation run. Values sum to 1.0
    (within floating-point tolerance).
    """
    graph = _build_graph(db_path)
    if graph.number_of_nodes() == 0:
        return {}
    return nx.pagerank(
        graph,
        alpha=_PAGERANK_ALPHA,
        max_iter=_PAGERANK_MAX_ITER,
        tol=_PAGERANK_TOL,
    )


def _weighted_sum_from_graph(
    graph: nx.DiGraph, pagerank: dict[str, float]
) -> dict[str, float]:
    """Return ``{slug: pr_ws}`` given a prebuilt graph + pagerank dict.

    Extracted so tests can share the graph construction without
    re-hitting sqlite. Pure function over the inputs.
    """
    scores: dict[str, float] = {}
    for node in graph.nodes:
        # Bidirectional neighborhood: predecessors ∪ successors. The
        # calibration note showed forward-only gives AUC=0.46 (worse
        # than random); bidirectional is the correct variant.
        neighbors = set(graph.predecessors(node)) | set(graph.successors(node))
        total = 0.0
        for neighbor in neighbors:
            pr = pagerank.get(neighbor, 0.0)
            out_deg = graph.out_degree(neighbor)
            if out_deg > 0:
                total += pr / out_deg
        scores[node] = total
    return scores


def compute_weighted_sum(db_path: Path) -> dict[str, float]:
    """Return ``{slug: pr_ws}`` for every note in ``cortex-index.db``.

    ``pr_ws(N) = Σ_{M ∈ neighbors(N)} pagerank(M) / out_degree(M)``,
    bidirectional neighbors. Isolated notes (no structural links in
    either direction) score 0.0.
    """
    graph = _build_graph(db_path)
    if graph.number_of_nodes() == 0:
        return {}
    pagerank = nx.pagerank(
        graph,
        alpha=_PAGERANK_ALPHA,
        max_iter=_PAGERANK_MAX_ITER,
        tol=_PAGERANK_TOL,
    )
    return _weighted_sum_from_graph(graph, pagerank)


def linkedness_tier(score: float) -> LinkednessTier:
    """Map a ``pr_ws`` score to a binary linkedness tier.

    - ``score == 0.0`` → ``"isolated"`` (no structural links at all;
      57.5% of the current vault).
    - ``score > 0.0`` → ``"linked"``.

    The calibration note shows 95.3% of notes fall below 0.001, so a
    finer-grained tier system (isolated / weak / medium / strong / hub)
    would put almost everything in one bucket today. The binary tier is
    what's actually informative for downstream consumers (vault_health
    event, decay-recovery routing).

    TODO: revisit when vault topology is dense enough that a non-trivial
    fraction of notes exceed the 0.001 weak/medium boundary — at that
    point ``LinkednessTier`` should be widened to the multi-tier
    Literal in the design note and this function should map the
    calibrated thresholds.
    """
    if score > 0.0:
        return "linked"
    return "isolated"


def tier_counts(scores: dict[str, float]) -> dict[LinkednessTier, int]:
    """Return ``{tier: count}`` summarising a slug→score map.

    Used by ``vault_health`` to drop a single dict into the
    ``pagerank_linkedness`` event field.
    """
    counts: dict[LinkednessTier, int] = {"isolated": 0, "linked": 0}
    for score in scores.values():
        counts[linkedness_tier(score)] += 1
    return counts
