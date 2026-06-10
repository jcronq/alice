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

PageRank implementation: pure-Python power iteration over a
predecessor adjacency map. The spec's original snippet showed networkx
but explicitly noted ``pure Python — no networkx dependency`` in the
section header; the alice-venv does not ship networkx (and shouldn't,
just for one one-page algorithm) so the dependency was dropped.
Convergence empirically observed at 50-52 iterations on the live vault
(2,153 nodes, ~2,600 edges); <1s total runtime.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

# Tier labels exposed to downstream consumers (vault_health event,
# domain inference). Binary today; see TODO inside ``linkedness_tier``.
LinkednessTier = Literal["isolated", "linked"]

# PageRank algorithm parameters. Damping=0.85 is the textbook default
# (Brin & Page 1998); the validation pipeline used the same value.
# tol=1e-6 / max_iter=100 converged in ~50 iters on the live vault.
_PAGERANK_ALPHA = 0.85
_PAGERANK_MAX_ITER = 100
_PAGERANK_TOL = 1.0e-6


def _build_graph(
    db_path: Path,
) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Load structural-link graph from ``cortex-index.db``.

    Returns ``(nodes, forward, reverse)`` where ``nodes`` is every
    distinct slug seen, ``forward[s]`` is the list of slugs ``s`` links
    TO (out-edges), and ``reverse[s]`` is the list of slugs that link
    to ``s`` (in-edges). Slugs from the ``notes`` table register as
    nodes even with no edges; slugs that appear only as link targets
    (not yet ingested as notes) are registered too — that's harmless
    for PageRank because they sit as dangling pseudo-nodes.

    The ``resolved`` column is intentionally NOT filtered on — see the
    module docstring for the rationale.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        nodes_set: set[str] = set()
        forward: dict[str, list[str]] = {}
        reverse: dict[str, list[str]] = {}

        for (slug,) in conn.execute("SELECT slug FROM notes"):
            nodes_set.add(slug)
            forward.setdefault(slug, [])
            reverse.setdefault(slug, [])

        for source_slug, target_slug in conn.execute(
            "SELECT source_slug, target_slug FROM links WHERE is_structural = 1"
        ):
            nodes_set.add(source_slug)
            nodes_set.add(target_slug)
            forward.setdefault(source_slug, []).append(target_slug)
            reverse.setdefault(source_slug, [])
            forward.setdefault(target_slug, [])
            reverse.setdefault(target_slug, []).append(source_slug)

        # Stable iteration order for reproducibility — sorting once
        # here avoids set-iteration order leaking into the pagerank
        # convergence test below.
        return sorted(nodes_set), forward, reverse
    finally:
        conn.close()


def _pagerank_power_iteration(
    nodes: list[str],
    forward: dict[str, list[str]],
) -> dict[str, float]:
    """Pure-Python power iteration over the forward adjacency map.

    Returns ``{slug: pr_value}``. Values sum to 1.0 within
    floating-point tolerance.

    Handles dangling nodes (slugs with no out-edges) by redistributing
    their rank mass uniformly across all nodes — the standard textbook
    correction. Without this, dangling nodes would absorb the entire
    rank mass over iterations.
    """
    n = len(nodes)
    if n == 0:
        return {}

    initial = 1.0 / n
    pr: dict[str, float] = {node: initial for node in nodes}
    teleport = (1.0 - _PAGERANK_ALPHA) / n

    for _iteration in range(_PAGERANK_MAX_ITER):
        # Dangling-node correction: every node with no out-edges
        # spreads its current rank uniformly to all nodes.
        dangling_mass = sum(
            pr[node] for node in nodes if not forward.get(node)
        )
        base = teleport + _PAGERANK_ALPHA * dangling_mass / n

        new_pr: dict[str, float] = {node: base for node in nodes}
        for source_node in nodes:
            successors = forward.get(source_node) or ()
            out_deg = len(successors)
            if out_deg == 0:
                continue
            contribution = _PAGERANK_ALPHA * pr[source_node] / out_deg
            for target in successors:
                if target in new_pr:
                    new_pr[target] += contribution

        diff = sum(abs(new_pr[node] - pr[node]) for node in nodes)
        pr = new_pr
        if diff < _PAGERANK_TOL:
            break

    return pr


def compute_pagerank(db_path: Path) -> dict[str, float]:
    """Return ``{slug: pagerank}`` over the structural-link graph.

    PageRank is computed with damping=0.85, max_iter=100, tol=1e-6 —
    the same parameters used in the validation run. Values sum to 1.0
    (within floating-point tolerance).
    """
    nodes, forward, _reverse = _build_graph(db_path)
    return _pagerank_power_iteration(nodes, forward)


def _weighted_sum_from_graph(
    nodes: list[str],
    forward: dict[str, list[str]],
    reverse: dict[str, list[str]],
    pagerank: dict[str, float],
) -> dict[str, float]:
    """Return ``{slug: pr_ws}`` given a prebuilt graph + pagerank dict.

    Extracted so tests can share the graph construction without
    re-hitting sqlite. Pure function over the inputs.
    """
    scores: dict[str, float] = {}
    for node in nodes:
        # Bidirectional neighborhood: predecessors ∪ successors. The
        # calibration note showed forward-only gives AUC=0.46 (worse
        # than random); bidirectional is the correct variant.
        neighbors = set(forward.get(node) or ()) | set(reverse.get(node) or ())
        total = 0.0
        for neighbor in neighbors:
            pr = pagerank.get(neighbor, 0.0)
            out_deg = len(forward.get(neighbor) or ())
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
    nodes, forward, reverse = _build_graph(db_path)
    if not nodes:
        return {}
    pagerank = _pagerank_power_iteration(nodes, forward)
    return _weighted_sum_from_graph(nodes, forward, reverse, pagerank)


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
