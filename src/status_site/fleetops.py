"""Worker-usage digest (port of the agentry-dashboard "fleetops" panel).

Turns a list of per-run records into the numbers Jason asked for:
cost, lead time, change-failure rate, CI pass rate — split by session
family (``primary`` vs ``other``) so dispatch lanes can be compared
(e.g. thinking vs Sonnet).

Input records are plain dicts; field names are matched leniently so the
bg-task registry, PR-duration data, and CI results can all feed it:

* ``lane`` / ``family`` / ``model`` — which dispatch lane ran it
* ``started`` / ``ended`` (ISO) or ``duration_s`` / ``duration_ms``
* ``cost_usd`` / ``total_cost_usd``
* ``failed`` (bool) or ``status`` in {"failed", "error", "reverted"} —
  counts toward change-failure rate
* ``ci_passed`` (bool, optional) — counts toward CI pass rate

Pure functions only; no I/O except ``read_jsonl`` (best-effort).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FAMILIES: tuple[str, ...] = ("primary", "other")
_FAILED_STATUSES = frozenset({"failed", "error", "reverted"})


def read_jsonl(path: Path) -> list[dict]:
    """Best-effort JSONL reader: missing file → ``[]``, bad lines skipped."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _epoch(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def duration_seconds(rec: dict) -> float | None:
    d = _num(rec.get("duration_s"))
    if d is None:
        ms = _num(rec.get("duration_ms"))
        d = None if ms is None else ms / 1000.0
    if d is None:
        a, b = _epoch(rec.get("started")), _epoch(rec.get("ended"))
        if a is not None and b is not None:
            d = b - a
    return d if d is not None and d >= 0 else None


def cost_usd(rec: dict) -> float | None:
    c = _num(rec.get("cost_usd"))
    return c if c is not None else _num(rec.get("total_cost_usd"))


def is_failed(rec: dict) -> bool:
    if rec.get("failed") is True:
        return True
    return str(rec.get("status") or "").lower() in _FAILED_STATUSES


def family_of(rec: dict, primary_lanes: Iterable[str]) -> str:
    """``primary`` when the record's lane/family/model is in ``primary_lanes``."""
    lane = str(rec.get("lane") or rec.get("family") or rec.get("model") or "")
    return "primary" if lane in set(primary_lanes) else "other"


def _summarize(recs: list[dict]) -> dict[str, Any]:
    durs = [d for d in map(duration_seconds, recs) if d is not None]
    costs = [c for c in map(cost_usd, recs) if c is not None]
    ci = [r["ci_passed"] for r in recs if isinstance(r.get("ci_passed"), bool)]
    n = len(recs)
    return {
        "runs": n,
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "cost_usd_per_run": round(sum(costs) / len(costs), 6) if costs else None,
        "lead_time_median_s": statistics.median(durs) if durs else None,
        "change_failure_rate": (sum(1 for r in recs if is_failed(r)) / n) if n else None,
        "ci_pass_rate": (sum(ci) / len(ci)) if ci else None,
    }


def digest(
    records: Iterable[dict],
    *,
    primary_lanes: Iterable[str] = (),
    since_epoch: float | None = None,
) -> dict[str, Any]:
    """Build ``{"overall": {...}, "families": {"primary": {...}, "other": {...}}}``.

    ``since_epoch`` drops records whose ``ended``/``started`` timestamp is
    older; records with no usable timestamp are kept. Rates are ``None``
    (not 0) when there is no data, so the UI can show "n/a" rather than a
    misleading perfect score.
    """
    primary = tuple(primary_lanes)
    kept: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if since_epoch is not None:
            t = _epoch(r.get("ended")) or _epoch(r.get("started"))
            if t is not None and t < since_epoch:
                continue
        kept.append(r)
    by_family: dict[str, list[dict]] = {f: [] for f in FAMILIES}
    for r in kept:
        by_family[family_of(r, primary)].append(r)
    return {
        "overall": _summarize(kept),
        "families": {f: _summarize(rs) for f, rs in by_family.items()},
    }
