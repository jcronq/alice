"""Stratified pass-rate aggregator for the speaking-benchmark.

Reads ``eval_results.jsonl`` (one row per ``(turn_id, candidate_id)``,
emitted by :mod:`alice_eval.bench`) and produces a stratified
pass-rate table per turn-category plus an aggregate, weighted by
category frequency in live traffic (same weights as
``CATEGORY_TARGETS`` in :mod:`alice_eval.sampling`).

Output schema (per the design's acceptance criteria):

    {
      "candidate_id": "qwen",
      "aggregate_pass_rate": 0.82,
      "weighted_pass_rate": 0.79,
      "by_category": {
        "tactical": {"pass": 9, "total": 10, "rate": 0.90},
        ...
      },
      "verdict": "ACCEPTABLE" | "NEEDS_WORK"
    }

The decision threshold is 0.75 on the weighted aggregate — the same
bar the original blind A/B design used so pre-/post- numbers are
comparable.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from alice_eval.sampling import CATEGORY_TARGETS

__all__ = [
    "DECISION_THRESHOLD",
    "ScoreReport",
    "format_report",
    "main_score",
    "score_results",
]

log = logging.getLogger(__name__)

DECISION_THRESHOLD = 0.75


@dataclass(slots=True)
class CategoryStat:
    passed: int = 0
    total: int = 0

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"pass": self.passed, "total": self.total, "rate": round(self.rate, 3)}


@dataclass(slots=True)
class ScoreReport:
    candidate_id: str
    aggregate_pass_rate: float
    weighted_pass_rate: float
    by_category: dict[str, CategoryStat] = field(default_factory=dict)
    verdict: str = "NEEDS_WORK"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "aggregate_pass_rate": round(self.aggregate_pass_rate, 3),
            "weighted_pass_rate": round(self.weighted_pass_rate, 3),
            "by_category": {
                cat: stat.to_dict() for cat, stat in self.by_category.items()
            },
            "verdict": self.verdict,
        }


def _category_weights() -> dict[str, float]:
    total = sum(CATEGORY_TARGETS.values()) or 1
    return {k: v / total for k, v in CATEGORY_TARGETS.items()}


def score_results(
    rows: Iterable[Mapping[str, Any]],
    *,
    candidate_id: str | None = None,
) -> ScoreReport:
    """Aggregate ``rows`` into a :class:`ScoreReport`.

    Each row is the dict written by ``alice_eval bench`` — at minimum
    ``turn_id``, ``category``, ``candidate_id``, ``resolved`` (bool).
    Rows whose ``candidate_id`` doesn't match the optional
    ``candidate_id`` filter are skipped.
    """
    by_category: dict[str, CategoryStat] = defaultdict(CategoryStat)
    matched_candidate: str | None = candidate_id
    n_total = 0
    n_passed = 0
    for row in rows:
        if candidate_id and row.get("candidate_id") != candidate_id:
            continue
        if matched_candidate is None:
            matched_candidate = row.get("candidate_id")
        cat = row.get("category") or "unknown"
        resolved = bool(row.get("resolved"))
        stat = by_category[cat]
        stat.total += 1
        n_total += 1
        if resolved:
            stat.passed += 1
            n_passed += 1

    aggregate = n_passed / n_total if n_total else 0.0

    weights = _category_weights()
    weighted_num = 0.0
    weighted_den = 0.0
    for cat, stat in by_category.items():
        w = weights.get(cat, 0.0)
        if stat.total == 0 or w == 0:
            continue
        weighted_num += w * stat.rate
        weighted_den += w
    weighted = weighted_num / weighted_den if weighted_den else aggregate

    return ScoreReport(
        candidate_id=matched_candidate or "unknown",
        aggregate_pass_rate=aggregate,
        weighted_pass_rate=weighted,
        by_category=dict(by_category),
        verdict=(
            "ACCEPTABLE" if weighted >= DECISION_THRESHOLD else "NEEDS_WORK"
        ),
    )


def load_results(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).expanduser().open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def format_report(report: ScoreReport) -> str:
    """Pretty-print the report as a fixed-width table + JSON tail."""
    lines = [
        f"=== alice-speak score: {report.candidate_id} ===",
        f"{'category':<16}{'pass':>6}{'total':>8}{'rate':>8}",
        "-" * 38,
    ]
    for cat in (
        list(CATEGORY_TARGETS.keys())
        + [c for c in report.by_category if c not in CATEGORY_TARGETS]
    ):
        stat = report.by_category.get(cat)
        if not stat or stat.total == 0:
            continue
        lines.append(
            f"{cat:<16}{stat.passed:>6}{stat.total:>8}{stat.rate:>8.3f}"
        )
    lines.append("-" * 38)
    lines.append(
        f"{'aggregate':<16}{'':>6}{'':>8}{report.aggregate_pass_rate:>8.3f}"
    )
    lines.append(
        f"{'weighted':<16}{'':>6}{'':>8}{report.weighted_pass_rate:>8.3f}"
    )
    lines.append(f"verdict: {report.verdict} (threshold {DECISION_THRESHOLD})")
    return "\n".join(lines)


def main_score(
    *,
    results_path: str | Path,
    out_path: str | Path | None = None,
    candidate_id: str | None = None,
) -> ScoreReport:
    rows = load_results(results_path)
    report = score_results(rows, candidate_id=candidate_id)
    print(format_report(report))
    if out_path:
        Path(out_path).expanduser().write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alice_eval.score",
        description="Stratified pass-rate scorer for the speaking-benchmark.",
    )
    p.add_argument("results", help="Path to eval_results.jsonl")
    p.add_argument(
        "--candidate",
        default=None,
        help="Filter to a single candidate_id (default: include all rows)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Optional path to write the JSON report (in addition to stdout)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    main_score(
        results_path=args.results,
        out_path=args.out,
        candidate_id=args.candidate,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
