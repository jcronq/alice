"""Speaking-benchmark driver — wires sample → instances → candidate
runs → assertion runner → results JSONL.

Provides the four ``eval speaking <sub>`` subcommands:

- ``sample``    — re-exports :func:`eval.sampling.main`
  for the SWE-Bench naming
- ``instances`` — derive per-turn assertion files from the sample
  (see :mod:`eval.instances`)
- ``run``       — for a single candidate, replay each instance,
  evaluate assertions, write ``eval_results.jsonl``
- ``score``     — stratified pass-rate aggregator (see
  :mod:`eval.score`)

Subset modes (per the design):

- ``full``     (default) — every instance
- ``lite``    — 10-turn fast-iteration filter:
  4 tactical + 2 design + 2 image + 2 conversational
- ``verified`` — only instances listed in ``verified.txt`` (one
  ``turn_id`` per line, hand-curated). Falls back to the full set
  with a stderr warning when the file is missing.

The runner reuses :mod:`eval.replay`'s candidate calling. We
load the same ``configs/eval_candidates.json``, filter to one
candidate by ``--candidate <id>``, and invoke
:func:`eval.replay.replay_turn` per instance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

from eval import assertions as _assertions
from eval import instances as _instances
from eval import sampling as _sampling
from eval import score as _score
from eval.prompt import build_system_prompt
from eval.replay import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TIMEOUT_S,
    Candidate,
    load_candidates,
    load_sample,
    load_speaking_log,
    replay_turn,
)

__all__ = [
    "SUBSETS",
    "build_arg_parser",
    "main",
    "main_run",
    "select_subset",
]

log = logging.getLogger(__name__)

# Lite subset shape per design table (N=10).
SUBSETS: dict[str, dict[str, int]] = {
    "lite": {
        "tactical": 4,
        "design": 2,
        "image": 2,
        "conversational": 2,
    },
    # `full` and `verified` are handled separately — no fixed-count quota.
}


def select_subset(
    sample: list[dict],
    subset: str,
    *,
    verified_path: str | Path | None = None,
) -> list[dict]:
    """Filter ``sample`` (the loaded eval_sample.jsonl rows) per the
    subset selector.

    ``full``     → return ``sample`` unchanged.
    ``verified`` → return only rows whose ``turn_id`` appears in
                   ``verified_path`` (defaults to ``verified.txt`` in
                   the cwd). Missing file → warn + fall through to
                   full.
    ``lite``     → take the first N from each category per
                   :data:`SUBSETS`.
    """
    subset = (subset or "full").lower()
    if subset == "full":
        return sample
    if subset == "verified":
        path = Path(verified_path or "verified.txt").expanduser()
        if not path.is_file():
            print(
                f"WARNING: --subset verified but {path} not found; "
                "running full set",
                file=sys.stderr,
            )
            return sample
        wanted = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        filtered = [r for r in sample if r.get("turn_id") in wanted]
        if not filtered:
            print(
                f"WARNING: no sample rows matched verified.txt "
                f"({len(wanted)} ids); running full set",
                file=sys.stderr,
            )
            return sample
        return filtered
    if subset == "lite":
        quota = dict(SUBSETS["lite"])
        picked: list[dict] = []
        for row in sample:
            cat = row.get("sampled_category") or row.get("category")
            if quota.get(cat, 0) > 0:
                picked.append(row)
                quota[cat] -= 1
        return picked
    raise ValueError(f"unknown subset {subset!r}")


# ---------------------------------------------------------------------------
# Run: replay + assertions → eval_results.jsonl


@dataclass(slots=True)
class _RunConfig:
    candidate: Candidate
    sample: list[dict]
    instances_dir: Path
    out_path: Path
    log_path: Path
    concurrency: int
    system_prompt: str


async def _run_one(
    cfg: _RunConfig,
    all_turns: list[dict],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    sample_row: dict,
) -> dict:
    async with semaphore:
        replay_result = await replay_turn(
            cfg.candidate,
            sample_row,
            all_turns,
            system_prompt=cfg.system_prompt,
            client=client,
            timeout_s=DEFAULT_TIMEOUT_S,
        )
    af_path = cfg.instances_dir / f"{replay_result.turn_id}.assert.json"
    if not af_path.is_file():
        log.warning(
            "no assertion file at %s for turn %s — skipping",
            af_path,
            replay_result.turn_id,
        )
        return {
            "turn_id": replay_result.turn_id,
            "candidate_id": cfg.candidate.id,
            "category": replay_result.category,
            "resolved": False,
            "skipped": True,
            "reason": "missing assertion file",
            "output": replay_result.output,
            "latency_ms": replay_result.latency_ms,
            "status": replay_result.status,
            "error": replay_result.error,
        }
    af = _assertions.load_assertion_file(af_path)
    instance_result = _assertions.evaluate_instance(
        af, replay_result.output, candidate_id=cfg.candidate.id
    )
    row = instance_result.to_dict()
    row["latency_ms"] = replay_result.latency_ms
    row["status"] = replay_result.status
    row["error"] = replay_result.error
    row["output"] = replay_result.output
    return row


async def _drive_run(cfg: _RunConfig) -> list[dict]:
    all_turns = load_speaking_log(cfg.log_path)
    semaphore = asyncio.Semaphore(cfg.concurrency)
    async with httpx.AsyncClient() as client:
        tasks = [
            _run_one(cfg, all_turns, client, semaphore, sample_row)
            for sample_row in cfg.sample
        ]
        results = await asyncio.gather(*tasks)
    return results


def main_run(
    *,
    candidate_id: str,
    sample_path: str | Path,
    candidates_path: str | Path,
    instances_dir: str | Path,
    out_path: str | Path,
    log_path: str | Path,
    subset: str = "full",
    verified_path: str | Path | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    personae_path: str | Path | None = None,
) -> list[dict]:
    """Drive one candidate through every selected instance and write
    ``eval_results.jsonl``. Returns the list of row dicts written."""
    candidates = load_candidates(candidates_path)
    matching = [c for c in candidates if c.id == candidate_id]
    if not matching:
        raise ValueError(
            f"candidate {candidate_id!r} not found in {candidates_path}; "
            f"available: {[c.id for c in candidates]}"
        )
    candidate = matching[0]
    sample = load_sample(sample_path)
    sample = select_subset(sample, subset, verified_path=verified_path)
    if not sample:
        raise ValueError("subset selection produced 0 instances")

    system_prompt = build_system_prompt(personae_path=personae_path)
    out_path_resolved = Path(out_path).expanduser()
    out_path_resolved.parent.mkdir(parents=True, exist_ok=True)

    cfg = _RunConfig(
        candidate=candidate,
        sample=sample,
        instances_dir=Path(instances_dir).expanduser(),
        out_path=out_path_resolved,
        log_path=Path(log_path).expanduser(),
        concurrency=concurrency,
        system_prompt=system_prompt,
    )
    rows = asyncio.run(_drive_run(cfg))

    with out_path_resolved.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    print(
        f"Wrote {len(rows)} result rows for candidate '{candidate.id}' "
        f"(subset={subset}) to {out_path_resolved}",
        file=sys.stderr,
    )
    return rows


# ---------------------------------------------------------------------------
# CLI plumbing for `eval speaking <sub>`


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval speaking",
        description="SWE-Bench-style speaking-benchmark (issue #237).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # `sample` — delegate to existing sampling.main
    sample_p = sub.add_parser(
        "sample",
        help="(alias for `python -m eval sample`)",
    )
    sample_p.add_argument("--log", default=str(_sampling.DEFAULT_LOG_PATH))
    sample_p.add_argument("--out", default=str(_sampling.DEFAULT_OUTPUT_PATH))
    sample_p.add_argument("--seed", type=int, default=_sampling.DEFAULT_SEED)
    sample_p.add_argument(
        "--lookback-days", type=int, default=_sampling.DEFAULT_LOOKBACK_DAYS
    )

    inst_p = sub.add_parser(
        "instances",
        help="Derive per-turn assertion files from the sample",
    )
    inst_p.add_argument("--sample", default="eval_sample.jsonl")
    inst_p.add_argument("--out-dir", default=str(_instances.DEFAULT_INSTANCES_DIR))
    inst_p.add_argument(
        "--bleu-threshold",
        type=float,
        default=_instances.DEFAULT_BLEU_THRESHOLD,
    )

    run_p = sub.add_parser(
        "run",
        help="Run a candidate against the instances, write eval_results.jsonl",
    )
    run_p.add_argument("--candidate", required=True)
    run_p.add_argument("--sample", default="eval_sample.jsonl")
    run_p.add_argument(
        "--candidates", default="configs/eval_candidates.json"
    )
    run_p.add_argument(
        "--instances-dir", default=str(_instances.DEFAULT_INSTANCES_DIR)
    )
    run_p.add_argument("--out", default="eval_results.jsonl")
    run_p.add_argument(
        "--log",
        default="~/alice-mind/inner/state/speaking-turns.jsonl",
    )
    run_p.add_argument(
        "--subset",
        default="full",
        choices=("full", "lite", "verified"),
    )
    run_p.add_argument("--verified-path", default=None)
    run_p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run_p.add_argument("--personae", default=None)

    score_p = sub.add_parser(
        "score",
        help="Print the stratified pass-rate table for an eval_results.jsonl",
    )
    score_p.add_argument("results")
    score_p.add_argument("--candidate", default=None)
    score_p.add_argument("--out", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.cmd == "sample":
        _sampling.main_sample(
            log_path=args.log,
            out_path=args.out,
            seed=args.seed,
            lookback_days=args.lookback_days,
        )
        return 0
    if args.cmd == "instances":
        _instances.main_instances(
            sample_path=args.sample,
            out_dir=args.out_dir,
            bleu_threshold=args.bleu_threshold,
        )
        return 0
    if args.cmd == "run":
        main_run(
            candidate_id=args.candidate,
            sample_path=args.sample,
            candidates_path=args.candidates,
            instances_dir=args.instances_dir,
            out_path=args.out,
            log_path=args.log,
            subset=args.subset,
            verified_path=args.verified_path,
            concurrency=args.concurrency,
            personae_path=args.personae,
        )
        return 0
    if args.cmd == "score":
        _score.main_score(
            results_path=args.results,
            out_path=args.out,
            candidate_id=args.candidate,
        )
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# Helper re-export so `load_speaking_log` is reachable via this module.
def _passthrough_iter(rows: Iterable[dict]) -> list[dict]:
    return list(rows)
