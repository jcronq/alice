#!/usr/bin/env python3
"""GBrain typed-edge calibration harness.

Runs a weight-sweep over typed-edge boost parameters in the cue runner's
scoring pipeline. For each weight combination, scores a sample of queries,
computes P@3, Recall@10, MRR, and regression count, then identifies the
best and safest weight combos.

Usage:
    python3 typed_edge_calibration_harness.py \
        --db ~/alice-mind/inner/state/cortex-index.db \
        --queries queries.jsonl \
        [--output results.json]

Query format (one JSON object per line in queries.jsonl):
    {
        "id": "turn_001",
        "query": "structural recovery P0 pilot",
        "context_slugs": ["slug1", "slug2"],
        "gold_relevant": ["target-slug-1", "target-slug-2"],
        "label_confidence": "high"  // optional: "high" | "medium" | "heuristic"
    }

If gold_relevant is absent or empty, relevance is inferred from typed edges
(context slugs that have typed links to targets = relevant targets).
"""

import argparse
import json
import math
import pathlib
import sqlite3
import sys
import random
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    slug: str
    base_score: float  # -fts_rank (higher = better)
    hebbian_bonus: float = 0.0
    typed_bonus: float = 0.0
    final_score: float = 0.0


def _compose_fts_match(query: str) -> str:
    """Compose an FTS5 MATCH expression from raw tokens.

    Quote each token to neutralise FTS5 special characters and join with OR.
    """
    tokens = query.lower().split()
    quoted: list[str] = []
    for t in tokens:
        cleaned = t.replace('"', "").strip()
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted)


def query_fts(
    db: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[tuple[str, float]]:
    """Run FTS5 MATCH, return [(slug, fts_rank), ...] sorted by rank."""
    fts_expr = _compose_fts_match(query)
    rows = db.execute(
        """
        SELECT n.slug, notes_fts.rank AS fts_rank
        FROM notes_fts
        JOIN notes n ON notes_fts.rowid = n.rowid
        WHERE notes_fts MATCH ?
        ORDER BY notes_fts.rank
        LIMIT ?
        """,
        (fts_expr, limit),
    ).fetchall()
    return rows


def query_edge_weights(
    db: sqlite3.Connection,
    context_slugs: list[str],
    structural_weight: float = 1.0,
    casual_weight: float = 0.25,
) -> dict[str, float]:
    """Hebbian edge-weight boost from the links table."""
    if not context_slugs:
        return {}
    placeholders = ",".join("?" * len(context_slugs))
    try:
        rows = db.execute(
            f"""
            SELECT target_slug,
                   SUM(CASE WHEN is_structural=1 THEN ? ELSE ? END)
                       AS edge_weight_sum
            FROM links
            WHERE resolved=1 AND source_slug IN ({placeholders})
            GROUP BY target_slug
            HAVING edge_weight_sum > 0
            """,
            (structural_weight, casual_weight, *context_slugs),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: float(row[1]) for row in rows}


def query_typed_edge_weights(
    db: sqlite3.Connection,
    context_slugs: list[str],
    cites_weight: float = 1.5,
    connects_to_weight: float = 0.5,
) -> dict[str, float]:
    """Typed-edge boost from the typed_edges table.

    Sums per-target typed edges where the source is one of the context slugs.
    `cites` edges contribute cites_weight; `connects_to` edges contribute
    connects_to_weight. Only edges with confidence='high' count.
    """
    if not context_slugs:
        return {}
    placeholders = ",".join("?" * len(context_slugs))
    try:
        rows = db.execute(
            f"""
            SELECT to_slug,
                   SUM(CASE WHEN link_type='cites' THEN ?
                             WHEN link_type='connects_to' THEN ?
                             ELSE 0 END) AS typed_weight_sum
            FROM typed_edges
            WHERE from_slug IN ({placeholders})
              AND confidence='high'
              AND link_source='predicate'
            GROUP BY to_slug
            HAVING typed_weight_sum > 0
            """,
            (cites_weight, connects_to_weight, *context_slugs),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: float(row[1]) for row in rows}


def score_query(
    db: sqlite3.Connection,
    query: str,
    context_slugs: list[str],
    *,
    hebbian_edge_boost: float = 0.5,
    hebbian_structural_weight: float = 1.0,
    hebbian_casual_weight: float = 0.25,
    hebbian_min_floor: int = 2,
    cites_weight: float = 1.5,
    connects_to_weight: float = 0.5,
    access_counts: Optional[dict[str, int]] = None,
) -> list[Candidate]:
    """Score a single query with the full cue runner pipeline.

    Returns candidates sorted by final_score descending.
    """
    fts_rows = query_fts(db, query, limit=50)
    edge_weights = query_edge_weights(
        db, context_slugs,
        structural_weight=hebbian_structural_weight,
        casual_weight=hebbian_casual_weight,
    )
    typed_weights = query_typed_edge_weights(
        db, context_slugs,
        cites_weight=cites_weight,
        connects_to_weight=connects_to_weight,
    )

    candidates: list[Candidate] = []
    for slug, fts_rank in fts_rows:
        base_score = -float(fts_rank)
        hebbian_bonus = 0.0
        if hebbian_edge_boost > 0 and slug in edge_weights:
            ew = edge_weights[slug]
            hebbian_bonus = hebbian_edge_boost * ew
            if ew >= hebbian_min_floor:
                hebbian_bonus += hebbian_edge_boost * 2.0

        typed_bonus = 0.0
        if slug in typed_weights:
            typed_bonus = typed_weights[slug]

        final_score = base_score + hebbian_bonus + typed_bonus
        candidates.append(Candidate(
            slug=slug,
            base_score=base_score,
            hebbian_bonus=hebbian_bonus,
            typed_bonus=typed_bonus,
            final_score=final_score,
        ))

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    candidates: list[Candidate],
    gold_relevant: set[str],
    top_k: int = 10,
) -> dict:
    """Compute P@3, Recall@10, MRR for a scored result list."""
    top_candidates = candidates[:top_k]
    top3 = candidates[:3]

    # Precision@3
    hits_at_3 = sum(1 for c in top3 if c.slug in gold_relevant)
    p_at_3 = hits_at_3 / 3.0 if top3 else 0.0

    # Recall@10
    hits_at_10 = sum(1 for c in top_candidates if c.slug in gold_relevant)
    recall_at_10 = hits_at_10 / len(gold_relevant) if gold_relevant else 0.0

    # MRR
    mrr = 0.0
    for i, c in enumerate(top_candidates):
        if c.slug in gold_relevant:
            mrr = 1.0 / (i + 1)
            break

    # Regression count: how many gold-relevant candidates dropped out of top-3
    # when comparing typed vs baseline (hebbian only)
    return {
        "p_at_3": round(p_at_3, 4),
        "recall_at_10": round(recall_at_10, 4),
        "mrr": round(mrr, 4),
        "hits_at_3": hits_at_3,
        "hits_at_10": hits_at_10,
    }


def compute_regression_count(
    baseline_candidates: list[Candidate],
    typed_candidates: list[Candidate],
    gold_relevant: set[str],
    top_k: int = 3,
) -> int:
    """Count gold-relevant candidates that dropped out of top-k when typed
    edges were applied (compared to baseline hebbian-only)."""
    baseline_top = set(c.slug for c in baseline_candidates[:top_k])
    typed_top = set(c.slug for c in typed_candidates[:top_k])
    relevant_in_baseline = baseline_top & gold_relevant
    relevant_in_typed = typed_top & gold_relevant
    return len(relevant_in_baseline - relevant_in_typed)


# ---------------------------------------------------------------------------
# Weight sweep
# ---------------------------------------------------------------------------

WEIGHT_GRID = [
    (0.5, 0.1), (1.0, 0.1), (1.5, 0.1), (2.0, 0.1), (3.0, 0.1),
    (0.5, 0.25), (1.0, 0.25), (1.5, 0.25), (2.0, 0.25), (3.0, 0.25),
    (0.5, 0.5), (1.0, 0.5), (1.5, 0.5), (2.0, 0.5), (3.0, 0.5),
    (0.5, 0.75), (1.0, 0.75), (1.5, 0.75), (2.0, 0.75), (3.0, 0.75),
    (0.5, 1.0), (1.0, 1.0), (1.5, 1.0), (2.0, 1.0), (3.0, 1.0),
]

BASELINE = (1.5, 0.5)


def run_sweep(
    db_path: str,
    queries: list[dict],
    output_path: Optional[str] = None,
    seed: int = 42,
) -> dict:
    """Run the full weight sweep.

    Args:
        db_path: Path to the cortex-index.db.
        queries: List of query dicts (see module docstring).
        output_path: Optional path to write JSON results.
        seed: Random seed for reproducible sampling.

    Returns:
        Results dict with per-combo metrics and recommendations.
    """
    random.seed(seed)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # Pre-load access counts for recency boost
    access_counts = {}
    try:
        for row in db.execute(
            "SELECT slug, access_count FROM note_metrics"
        ).fetchall():
            access_counts[row[0]] = row[1]
    except sqlite3.Error:
        pass

    # Score baseline (hebbian only, no typed edges)
    baseline_results = {}
    typed_results = {}

    for combo in WEIGHT_GRID:
        cites_w, connects_w = combo
        baseline_metrics = []
        typed_metrics = []
        regression_counts = []

        for q in queries:
            qid = q.get("id", "unknown")
            query_text = q["query"]
            context = q.get("context_slugs", [])
            gold = set(q.get("gold_relevant", []))

            # Baseline: hebbian only
            baseline_cands = score_query(
                db, query_text, context,
                cites_weight=0.0, connects_to_weight=0.0,
                access_counts=access_counts,
            )
            baseline_metrics.append(compute_metrics(baseline_cands, gold))

            # Typed: hebbian + typed edges
            typed_cands = score_query(
                db, query_text, context,
                cites_weight=cites_w,
                connects_to_weight=connects_w,
                access_counts=access_counts,
            )
            typed_metrics.append(compute_metrics(typed_cands, gold))

            # Regression
            if gold:
                reg = compute_regression_count(
                    baseline_cands, typed_cands, gold
                )
                regression_counts.append(reg)

        # Aggregate metrics for this combo
        avg_p3 = sum(m["p_at_3"] for m in typed_metrics) / len(typed_metrics)
        avg_recall = sum(m["recall_at_10"] for m in typed_metrics) / len(typed_metrics)
        avg_mrr = sum(m["mrr"] for m in typed_metrics) / len(typed_metrics)
        avg_regressions = (
            sum(regression_counts) / len(regression_counts)
            if regression_counts else 0
        )

        baseline_avg_p3 = (
            sum(m["p_at_3"] for m in baseline_metrics) / len(baseline_metrics)
        )
        p3_delta = avg_p3 - baseline_avg_p3

        typed_results[combo] = {
            "cites_weight": cites_w,
            "connects_to_weight": connects_w,
            "p_at_3": round(avg_p3, 4),
            "recall_at_10": round(avg_recall, 4),
            "mrr": round(avg_mrr, 4),
            "regressions": round(avg_regressions, 4),
            "p3_delta_vs_baseline": round(p3_delta, 4),
            "n_queries": len(queries),
        }

    # Find best and safe combos
    best = max(typed_results.values(), key=lambda r: r["p_at_3"])
    safe_candidates = [
        r for r in typed_results.values()
        if r["regressions"] == 0 and abs(r["p3_delta_vs_baseline"]) < 0.05
    ]
    safe = max(safe_candidates, key=lambda r: r["p_at_3"]) if safe_candidates else None

    # Recommendation
    meets_criteria = (
        best["p3_delta_vs_baseline"] >= 0.03
        and best["regressions"] <= 2
        and best["mrr"] >= 0.01
    )

    results = {
        "n_queries": len(queries),
        "n_weight_combos": len(WEIGHT_GRID),
        "baseline_p_at_3": round(
            sum(
                compute_metrics(
                    score_query(db, q["query"], q.get("context_slugs", []),
                                cites_weight=0.0, connects_to_weight=0.0),
                    set(q.get("gold_relevant", [])),
                )["p_at_3"]
                for q in queries
            ) / len(queries), 4
        ),
        "best_combo": {**best, "recommendation": "deploy" if meets_criteria else "hold"},
        "safe_combo": {**safe, "recommendation": "safe_deploy"} if safe else None,
        "all_combos": {f"{r['cites_weight']}-{r['connects_to_weight']}": r
                       for r in typed_results.values()},
        "meets_deploy_criteria": meets_criteria,
    }

    db.close()

    if output_path:
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GBrain typed-edge calibration harness"
    )
    parser.add_argument(
        "--db", required=True,
        help="Path to cortex-index.db",
    )
    parser.add_argument(
        "--queries", required=True,
        help="Path to queries.jsonl (one JSON object per line)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to write JSON results (optional)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Limit to N queries (for quick testing)",
    )
    args = parser.parse_args()

    # Load queries
    queries = []
    with open(args.queries) as f:
        for line in f:
            line = line.strip()
            if line:
                queries.append(json.loads(line))

    if args.sample:
        random.seed(args.seed)
        queries = random.sample(queries, min(args.sample, len(queries)))

    print(f"Loaded {len(queries)} queries from {args.queries}")
    print(f"Running sweep over {len(WEIGHT_GRID)} weight combinations...")

    results = run_sweep(
        db_path=args.db,
        queries=queries,
        output_path=args.output,
        seed=args.seed,
    )

    print(f"\nBaseline P@3: {results['baseline_p_at_3']}")
    print(f"Best combo: cites={results['best_combo']['cites_weight']}, "
          f"connects_to={results['best_combo']['connects_to_weight']}")
    print(f"  P@3: {results['best_combo']['p_at_3']} "
          f"(delta: {results['best_combo']['p3_delta_vs_baseline']:+.4f})")
    print(f"  MRR: {results['best_combo']['mrr']}")
    print(f"  Regressions: {results['best_combo']['regressions']}")
    print(f"  Recommendation: {results['best_combo']['recommendation']}")

    if results['safe_combo']:
        print(f"\nSafe combo: cites={results['safe_combo']['cites_weight']}, "
              f"connects_to={results['safe_combo']['connects_to_weight']}")
        print(f"  P@3: {results['safe_combo']['p_at_3']}")
        print(f"  Recommendation: {results['safe_combo']['recommendation']}")

    print(f"\nMeets deploy criteria: {results['meets_deploy_criteria']}")
    if args.output:
        print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
