"""Stage D review — append-only JSONL storage helpers + fixture seeder.

Two files in ``~/alice-mind/inner/state/``:

- ``stage-d-attempts.jsonl`` — written by thinking; one line per synthesis
  attempt. Append-only; the viewer never mutates it.
- ``stage-d-labels.jsonl`` — written by the viewer; one line per Jason
  label event. Append-only too; newest line per ``attempt_id`` wins on
  read so re-labeling is just another append.

Schema is documented in ``STAGE_D_SCHEMA.md`` next to this module.

Run ``python -m alice_viewer.stage_d_store --regen-fixtures`` to seed
sample attempts for UI development. Fixture writer refuses to clobber
real data — see ``regenerate_fixtures``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any, Iterable


ATTEMPTS_FILENAME = "stage-d-attempts.jsonl"
LABELS_FILENAME = "stage-d-labels.jsonl"
FIXTURE_ID_PREFIX = "att-fixture-"


# ---------------------------------------------------------------------------
# Path resolution


def state_dir(mind_dir: pathlib.Path) -> pathlib.Path:
    """Return ``<mind>/inner/state``. Created on demand."""
    p = pathlib.Path(mind_dir) / "inner" / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def attempts_path(mind_dir: pathlib.Path) -> pathlib.Path:
    return state_dir(mind_dir) / ATTEMPTS_FILENAME


def labels_path(mind_dir: pathlib.Path) -> pathlib.Path:
    return state_dir(mind_dir) / LABELS_FILENAME


# ---------------------------------------------------------------------------
# Read


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    """Read a JSONL file. Missing file → empty list. Bad lines skipped."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def read_attempts(mind_dir: pathlib.Path) -> list[dict[str, Any]]:
    return _read_jsonl(attempts_path(mind_dir))


def read_labels(mind_dir: pathlib.Path) -> list[dict[str, Any]]:
    return _read_jsonl(labels_path(mind_dir))


def latest_label_by_attempt(
    labels: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collapse a label log into ``{attempt_id: latest_label_record}``.

    JSONL is appended in time order, so a plain forward iteration with
    overwrite gives "newest wins". Records missing ``attempt_id`` are
    dropped silently.
    """
    out: dict[str, dict[str, Any]] = {}
    for rec in labels:
        aid = rec.get("attempt_id")
        if not aid:
            continue
        out[aid] = rec
    return out


def join_attempts_with_labels(
    attempts: Iterable[dict[str, Any]],
    labels: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a list of attempts, each with ``label_record`` attached.

    ``label_record`` is the newest label record for the attempt, or
    ``None`` if Jason hasn't labeled it. The original attempt dicts are
    not mutated — we copy each shallow-ly and add the field.
    """
    latest = latest_label_by_attempt(labels)
    out: list[dict[str, Any]] = []
    for att in attempts:
        rec = dict(att)
        aid = rec.get("id")
        rec["label_record"] = latest.get(aid) if aid else None
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Filter / sort / summarize


# Outcomes considered "drop" for filtering purposes.
DROP_OUTCOMES = {"dropped_agreement_reject", "dropped_disagreement_exhausted"}


def is_disagreement(att: dict[str, Any]) -> bool:
    """Disagreement = pending OR retry_history non-empty (already had a re-run)."""
    if att.get("outcome") == "disagreement_pending":
        return True
    rh = att.get("retry_history") or []
    return bool(rh)


def is_unlabeled(att: dict[str, Any]) -> bool:
    rec = att.get("label_record")
    if not rec:
        return True
    return rec.get("label") in (None, "", "unlabeled")


def filter_attempts(
    attempts: list[dict[str, Any]],
    *,
    since: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Filter joined attempts by date floor + status.

    ``since`` is an ISO date (YYYY-MM-DD); attempts with ``created_at``
    earlier than midnight-local of that date are dropped. Bad/missing
    ``created_at`` is treated as "keep".

    ``status`` is one of {disagreement, shipped, dropped, unlabeled} —
    matches the values exposed by ``/api/stage-d-attempts``.
    """
    out = list(attempts)

    if since:
        # Compare ISO-prefix ("YYYY-MM-DD" lexicographic) against the
        # leading 10 chars of created_at. Avoids tz parsing and is good
        # enough for daily filtering.
        floor = since[:10]
        out = [a for a in out if (a.get("created_at") or "")[:10] >= floor]

    if status == "disagreement":
        out = [a for a in out if is_disagreement(a)]
    elif status == "shipped":
        out = [a for a in out if a.get("outcome") == "shipped"]
    elif status == "dropped":
        out = [a for a in out if a.get("outcome") in DROP_OUTCOMES]
    elif status == "unlabeled":
        out = [a for a in out if is_unlabeled(a)]

    return out


def default_sort(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Default sort: disagreements first, then shipped, then dropped.

    Within each band, newest ``created_at`` first.
    """

    def band(a: dict[str, Any]) -> int:
        if is_disagreement(a):
            return 0
        if a.get("outcome") == "shipped":
            return 1
        return 2

    return sorted(
        attempts,
        key=lambda a: (band(a), -_ts_key(a.get("created_at"))),
    )


def _ts_key(s: str | None) -> float:
    """Best-effort ISO8601 → unix-ts. Bad input → 0 (oldest)."""
    if not s:
        return 0.0
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


def summarize(attempts: list[dict[str, Any]]) -> dict[str, int]:
    """Counts banner: ``{shipped, dropped, disagreement, total, unlabeled}``."""
    total = len(attempts)
    shipped = sum(1 for a in attempts if a.get("outcome") == "shipped")
    dropped = sum(1 for a in attempts if a.get("outcome") in DROP_OUTCOMES)
    disagreement = sum(1 for a in attempts if is_disagreement(a))
    unlabeled = sum(1 for a in attempts if is_unlabeled(a))
    return {
        "shipped": shipped,
        "dropped": dropped,
        "disagreement": disagreement,
        "total": total,
        "unlabeled": unlabeled,
    }


# ---------------------------------------------------------------------------
# Write (labels only — attempts log is read-only from the viewer's side)


def append_label(
    mind_dir: pathlib.Path,
    *,
    attempt_id: str,
    label: str,
    axes: dict[str, Any] | None = None,
    labeled_at: str | None = None,
) -> dict[str, Any]:
    """Append a single label record to ``stage-d-labels.jsonl``. Returns
    the record that was written.

    ``label`` is not strictly validated here — callers (the viewer
    endpoint) restrict it to a known set. ``axes`` may be omitted; if
    provided it's stored as-is. ``labeled_at`` defaults to ``now()`` in
    ISO8601 with local offset.
    """
    if not attempt_id or not isinstance(attempt_id, str):
        raise ValueError("attempt_id required")
    if not label or not isinstance(label, str):
        raise ValueError("label required")

    rec: dict[str, Any] = {
        "attempt_id": attempt_id,
        "label": label,
    }
    if axes:
        rec["label_axes"] = dict(axes)
    rec["labeled_at"] = labeled_at or dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    path = labels_path(mind_dir)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return rec


# ---------------------------------------------------------------------------
# Fixtures


# Built-in fixture attempts. All ids start with FIXTURE_ID_PREFIX so the
# safety check in ``regenerate_fixtures`` can distinguish synthetic data
# from real production attempts.
FIXTURES: list[dict[str, Any]] = [
    {
        "id": f"{FIXTURE_ID_PREFIX}001-shipped-clean",
        "pair": {
            "slug_a": "cortex-memory/research/2026-04-12-cluster-topology",
            "slug_b": "cortex-memory/people/jason",
        },
        "synthesis_text": (
            "Cluster-topology pair selection (w_norm < 0.50) produces the "
            "same semantic distance Jason describes when he says \"I want "
            "the model to find weird connections, not adjacent ones.\" The "
            "0.50 threshold is the operational form of that taste."
        ),
        "draft_attempt_n": 1,
        "qwen_verdict": {
            "tier": "T1",
            "novel": "yes",
            "reason": "Connects an algorithmic threshold to a taste-level user requirement.",
            "decision": "ship",
        },
        "haiku_verdict": {
            "tier": "T1",
            "novel": "yes",
            "reason": "Real connection — operationalizes a soft preference as a hard constraint.",
            "decision": "ship",
        },
        "outcome": "shipped",
        "retry_history": [],
        "created_at": "2026-05-09T03:12:04-04:00",
        "shipped_slug": "cortex-memory/research/2026-05-09-cluster-threshold-as-taste-operator",
        "jason_label": None,
    },
    {
        "id": f"{FIXTURE_ID_PREFIX}002-dropped-agree",
        "pair": {
            "slug_a": "cortex-memory/research/2026-04-26-stage-d-quality-rubric",
            "slug_b": "cortex-memory/reference/signal-formatting",
        },
        "synthesis_text": (
            "Both the Stage D quality rubric and Signal formatting guidance "
            "are forms of style guides. Therefore Stage D synthesis notes "
            "should follow Signal formatting conventions."
        ),
        "draft_attempt_n": 1,
        "qwen_verdict": {
            "tier": "T3",
            "novel": "no",
            "reason": "Forced abstract-level connection; categories don't transfer.",
            "decision": "reject",
        },
        "haiku_verdict": {
            "tier": "T3",
            "novel": "no",
            "reason": "Both are 'style guides' only at a level that makes the connection vacuous.",
            "decision": "reject",
        },
        "outcome": "dropped_agreement_reject",
        "retry_history": [],
        "created_at": "2026-05-09T03:18:41-04:00",
        "shipped_slug": None,
        "jason_label": None,
    },
    {
        "id": f"{FIXTURE_ID_PREFIX}003-disagreement-pending",
        "pair": {
            "slug_a": "cortex-memory/research/2026-04-25-sleep-architecture-design",
            "slug_b": "cortex-memory/research/2026-05-05-stage-d-quality-calibration",
        },
        "synthesis_text": (
            "If Stage D is a REM-analog meant to find unexpected "
            "recombinations, then a high judge-disagreement rate is the "
            "*signal* — it's where the model is doing exactly what REM "
            "exists to do. Calibrating away from disagreement would "
            "calibrate away from REM."
        ),
        "draft_attempt_n": 2,
        "qwen_verdict": {
            "tier": "T1",
            "novel": "yes",
            "reason": "Inverts the conventional reading of the calibration metric.",
            "decision": "ship",
        },
        "haiku_verdict": {
            "tier": "T2",
            "novel": "no",
            "reason": "The argument is sharp but the framing has appeared in prior notes.",
            "decision": "reject",
        },
        "outcome": "disagreement_pending",
        "retry_history": [
            "If Stage D is a REM-analog, judge disagreement might be a feature, not a bug.",
        ],
        "created_at": "2026-05-09T03:24:17-04:00",
        "shipped_slug": None,
        "jason_label": None,
    },
    {
        "id": f"{FIXTURE_ID_PREFIX}004-dropped-disagree-exhausted",
        "pair": {
            "slug_a": "cortex-memory/research/2026-05-08-stage-d-drought-pattern-analysis",
            "slug_b": "cortex-memory/projects/cozyhem",
        },
        "synthesis_text": (
            "Stage D droughts at low corpus density mirror CozyHem's "
            "automation gaps when entity coverage is sparse — both are "
            "manifestations of a coverage threshold below which "
            "generative work stalls."
        ),
        "draft_attempt_n": 3,
        "qwen_verdict": {
            "tier": "T2",
            "novel": "yes",
            "reason": "Pattern-matches at a useful abstraction level.",
            "decision": "ship",
        },
        "haiku_verdict": {
            "tier": "T3",
            "novel": "no",
            "reason": "Coverage-threshold framing is too generic to be load-bearing.",
            "decision": "reject",
        },
        "outcome": "dropped_disagreement_exhausted",
        "retry_history": [
            "Stage D droughts and CozyHem automation gaps both come from low coverage.",
            "Both Stage D and CozyHem hit a generative floor when entity density is low.",
        ],
        "created_at": "2026-05-09T03:31:02-04:00",
        "shipped_slug": None,
        "jason_label": None,
    },
]


def regenerate_fixtures(mind_dir: pathlib.Path) -> int:
    """Replace the attempts log with the built-in fixtures.

    Refuses to run if the existing attempts log has any record whose id
    doesn't start with ``FIXTURE_ID_PREFIX`` — that's the signal that
    real (thinking-written) data is in the file. In that case the caller
    must remove the file by hand if they really want to clobber it.

    Returns the number of fixture lines written.
    """
    path = attempts_path(mind_dir)
    existing = _read_jsonl(path)
    real = [r for r in existing if not str(r.get("id", "")).startswith(FIXTURE_ID_PREFIX)]
    if real:
        raise RuntimeError(
            f"refusing to overwrite {path}: contains "
            f"{len(real)} non-fixture record(s). "
            f"Delete the file manually if you truly want to clobber it."
        )
    # Safe to rewrite — only fixtures (or empty) present.
    with path.open("w", encoding="utf-8") as f:
        for rec in FIXTURES:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(FIXTURES)


# ---------------------------------------------------------------------------
# CLI


def _resolve_mind_dir(arg: str | None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg).expanduser()
    import os

    env = os.environ.get("ALICE_MIND")
    if env:
        return pathlib.Path(env).expanduser()
    return pathlib.Path.home() / "alice-mind"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stage D review storage utilities (fixture seeder, dump)."
    )
    ap.add_argument(
        "--mind-dir",
        help="Override mind directory (defaults to $ALICE_MIND or ~/alice-mind).",
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("regen-fixtures", help="Replace attempts log with built-in fixtures.")
    sub.add_parser("dump", help="Dump joined attempts as JSON to stdout.")
    # Back-compat: allow plain --regen-fixtures with no subcommand.
    ap.add_argument(
        "--regen-fixtures",
        action="store_true",
        help="Same as the regen-fixtures subcommand (kept for convenience).",
    )

    args = ap.parse_args(argv)
    mind = _resolve_mind_dir(args.mind_dir)

    if args.regen_fixtures or args.cmd == "regen-fixtures":
        n = regenerate_fixtures(mind)
        print(f"wrote {n} fixture attempts to {attempts_path(mind)}")
        return 0
    if args.cmd == "dump":
        joined = join_attempts_with_labels(read_attempts(mind), read_labels(mind))
        print(json.dumps(joined, indent=2, ensure_ascii=False))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
