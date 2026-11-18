"""Per-item stage-run attribution for the status-site timeline (slice timeline).

Each Waiting-on-you / In-flight item has an implicit lifecycle that cuts
across five stages: ``design`` → ``build`` → ``review`` → ``check`` →
``merge``. When Alice spawns a worker (Agent / Bash bg-task) in service of
an item — or CI closes a loop on it, or a PR merges — the caller records a
stage-run here. The status site reads back an aggregate and lights up the
dots inside the WoY expanded card.

Storage is an append-only JSONL sidecar in ``cfg.status_dir`` (same pattern
as ``.woy-dismissed.jsonl`` / ``.woy-effort.jsonl``). One line per run::

    {
      "item_slug": "slice-timeline",
      "stage": "build",
      "task_id": "bg-task-1234",
      "started_at": "2026-11-17T09:00:00-05:00",
      "ended_at":   "2026-11-17T09:12:00-05:00",   # null while running
      "status":     "success",                      # running | success | failed
      "summary":    "worker returned draft PR #890"
    }

The ``ended_at`` + ``status`` fields are updated by appending another line
with the same ``task_id`` — last write wins per ``(item_slug, task_id)``.
A record with no ``task_id`` is treated as a one-shot event (e.g. a merge
timestamp); every one-shot is its own line and they all show up in the
run list for that stage. Malformed lines are skipped so a bad write can't
poison the whole aggregate.

No historical retrofit: records only exist going forward from the point
callers start emitting them. An item with no records aggregates to five
``"none"`` stages, which the UI renders as five grey dots.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# File name of the append-only JSONL log, resolved relative to
# ``cfg.status_dir`` — same container as the dismiss / effort logs.
STAGE_RUNS_FILE = ".item-stage-runs.jsonl"

# Ordered lifecycle. The frontend renders one dot per stage in this
# order; the backend aggregate always returns five entries with this
# ordering (even for stages the item never touched) so the UI never
# has to guess whether an "empty" response means "no data yet" or
# "stage not relevant".
STAGES: tuple[str, ...] = ("design", "build", "review", "check", "merge")
# Fast membership probe (set) and index lookup (dict) off the ordered
# tuple so :func:`append_stage_run` can reject bad stages without a
# linear scan and :func:`aggregate_timeline` can emit stages in a
# stable order regardless of file iteration.
_STAGES_SET: frozenset[str] = frozenset(STAGES)

# Run status vocabulary. ``running`` means the run is in-flight (no
# ``ended_at`` yet, or ``ended_at`` set but status explicitly "running");
# ``success`` and ``failed`` are terminal. The UI rollup collapses the
# run history for a stage into one of {none, running, success, failed}.
STATUSES: tuple[str, ...] = ("running", "success", "failed")
_STATUSES_SET: frozenset[str] = frozenset(STATUSES)

# Defensive bound on the in-memory JSONL read. The log is append-only and
# older lines only matter when we care about per-task history; for the
# aggregate we only need the newest-per-task-per-stage entry, which fits
# easily under this cap even on an active repo.
STAGE_RUNS_MAX = 20_000

# Compiled slug-safety pattern — identical shape to ``safe_slug`` in
# ``server.py`` but kept local so this module has no runtime dep on it
# (tests import ``stage_runs`` directly). Mirror the 60-char cap used
# elsewhere.
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9-]")


def safe_slug(slug: object) -> str:
    """Narrow + cap an arbitrary input into ``[a-z0-9-]`` at most 60 chars."""
    return _SAFE_SLUG_RE.sub("", str(slug or "").lower())[:60].strip("-")


def safe_task_id(value: object) -> str:
    """Cap + narrow a task id. Accepts ``[A-Za-z0-9._-]`` up to 120 chars."""
    s = re.sub(r"[^A-Za-z0-9._-]", "", str(value or ""))[:120].strip("-._")
    return s


def now_iso() -> str:
    """Local time with a POSIX offset. Mirrors ``server.now_iso``."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass(frozen=True)
class StageRun:
    """One typed, normalised run record.

    The JSONL on disk stores the fields loosely; this dataclass is the
    in-process shape returned by :func:`read_stage_runs`. All optional
    fields default to the empty string / ``None`` so a one-shot event
    without a ``task_id`` / ``summary`` round-trips cleanly.
    """

    item_slug: str
    stage: str
    status: str
    started_at: str
    ended_at: str | None = None
    task_id: str = ""
    summary: str = ""

    def to_json(self) -> dict:
        """JSON-serialisable projection (what :func:`append_stage_run` writes)."""
        d: dict = {
            "item_slug": self.item_slug,
            "stage": self.stage,
            "status": self.status,
            "started_at": self.started_at,
        }
        if self.task_id:
            d["task_id"] = self.task_id
        if self.ended_at is not None:
            d["ended_at"] = self.ended_at
        if self.summary:
            d["summary"] = self.summary
        return d


def validate_stage(stage: object) -> str:
    """Return a lowercased stage or raise ``ValueError``."""
    s = str(stage or "").strip().lower()
    if s not in _STAGES_SET:
        raise ValueError(f"bad stage: {stage!r}")
    return s


def validate_status(status: object) -> str:
    """Return a lowercased status or raise ``ValueError``."""
    s = str(status or "").strip().lower()
    if s not in _STATUSES_SET:
        raise ValueError(f"bad status: {status!r}")
    return s


def append_stage_run(
    status_dir: Path,
    *,
    item_slug: str,
    stage: str,
    status: str,
    task_id: str = "",
    started_at: str = "",
    ended_at: str | None = None,
    summary: str = "",
) -> StageRun:
    """Append one stage-run record to ``STAGE_RUNS_FILE``.

    Validates stage / status, narrows slug + task_id to safe charsets,
    and fills in ``started_at`` with :func:`now_iso` when omitted. The
    returned :class:`StageRun` is the exact shape written to disk.

    ``ended_at`` of ``None`` means "still running"; callers emit a
    follow-up append with ``status`` set to ``success`` / ``failed``
    and the same ``task_id`` to close the run. The read side merges
    those by last-write-wins per ``(stage, task_id)``.
    """
    slug = safe_slug(item_slug)
    if not slug:
        raise ValueError(f"bad item_slug: {item_slug!r}")
    s = validate_stage(stage)
    st = validate_status(status)
    tid = safe_task_id(task_id)
    started = started_at.strip() if isinstance(started_at, str) else ""
    if not started:
        started = now_iso()
    ended = ended_at
    if isinstance(ended, str):
        ended = ended.strip() or None
    summary_norm = str(summary or "").strip()
    # Narrow summary to a reasonable render length — the UI shows this
    # in a tooltip, not a wall. 400 chars is long enough for a sentence
    # and short enough that a runaway caller can't fill the log.
    if len(summary_norm) > 400:
        summary_norm = summary_norm[:400].rstrip() + "…"
    rec = StageRun(
        item_slug=slug,
        stage=s,
        status=st,
        started_at=started,
        ended_at=ended,
        task_id=tid,
        summary=summary_norm,
    )
    status_dir = Path(status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    with open(status_dir / STAGE_RUNS_FILE, "a") as f:
        f.write(json.dumps(rec.to_json()) + "\n")
    return rec


def _parse_line(line: str) -> StageRun | None:
    """Parse one JSONL line into a :class:`StageRun`; ``None`` on any error."""
    try:
        v = json.loads(line)
    except ValueError:
        return None
    if not isinstance(v, dict):
        return None
    slug = safe_slug(v.get("item_slug"))
    if not slug:
        return None
    stage_raw = v.get("stage")
    if not isinstance(stage_raw, str) or stage_raw.strip().lower() not in _STAGES_SET:
        return None
    status_raw = v.get("status")
    if not isinstance(status_raw, str) or status_raw.strip().lower() not in _STATUSES_SET:
        return None
    started = v.get("started_at")
    if not isinstance(started, str) or not started.strip():
        return None
    ended_raw = v.get("ended_at")
    if ended_raw is None:
        ended: str | None = None
    elif isinstance(ended_raw, str) and ended_raw.strip():
        ended = ended_raw.strip()
    else:
        ended = None
    task_id = safe_task_id(v.get("task_id") or "")
    summary = v.get("summary") or ""
    if not isinstance(summary, str):
        summary = ""
    return StageRun(
        item_slug=slug,
        stage=stage_raw.strip().lower(),
        status=status_raw.strip().lower(),
        started_at=started.strip(),
        ended_at=ended,
        task_id=task_id,
        summary=summary,
    )


def read_stage_runs(
    status_dir: Path, item_slug: str | None = None
) -> list[StageRun]:
    """Load all stage-run records, optionally filtered by ``item_slug``.

    Records are returned in file order (append order). Malformed lines
    are skipped. When ``item_slug`` is set it is normalised via
    :func:`safe_slug` before matching, so the caller can pass whatever
    the frontend sent without worrying about case / punctuation drift.
    """
    p = Path(status_dir) / STAGE_RUNS_FILE
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return []
    want = safe_slug(item_slug) if item_slug is not None else None
    out: list[StageRun] = []
    for line in lines[-STAGE_RUNS_MAX:]:
        rec = _parse_line(line)
        if rec is None:
            continue
        if want is not None and rec.item_slug != want:
            continue
        out.append(rec)
    return out


def _collapse_tasked_runs(runs: Iterable[StageRun]) -> list[StageRun]:
    """Collapse runs that share a ``(stage, task_id)`` to the last one.

    Keeps one-shot events (``task_id == ""``) as independent entries so
    multiple merges / CI results on the same stage are each visible.
    Preserves input order of the first occurrence per keyed group,
    swapping the stored record for the newest one when a follow-up
    append closes the run.
    """
    by_key: dict[tuple[str, str], int] = {}
    collapsed: list[StageRun] = []
    for r in runs:
        if not r.task_id:
            collapsed.append(r)
            continue
        key = (r.stage, r.task_id)
        idx = by_key.get(key)
        if idx is None:
            by_key[key] = len(collapsed)
            collapsed.append(r)
        else:
            # Last write wins — close/retry updates the slot in place.
            collapsed[idx] = r
    return collapsed


def _rollup_status(runs: list[StageRun]) -> str:
    """Collapse a stage's run list to one of ``none / running / success / failed``.

    Rules (match the task's render spec):

    * no runs → ``none`` (UI dim/grey)
    * any run in ``running`` → ``running`` (UI pulse)
    * any run in ``success`` → ``success`` (UI bright)
    * else at least one ``failed`` → ``failed`` (UI red)
    """
    if not runs:
        return "none"
    statuses = {r.status for r in runs}
    if "running" in statuses:
        return "running"
    if "success" in statuses:
        return "success"
    if "failed" in statuses:
        return "failed"
    # Defensive: a run with some unseen status would fall through. The
    # validator in :func:`_parse_line` already drops malformed records,
    # so this branch is unreachable without a code change — but we keep
    # the fallback so a future status word doesn't silently crash the
    # aggregator.
    return "none"


def _latest(runs: list[StageRun]) -> StageRun | None:
    """Pick the latest run by ``started_at`` ISO string, breaking ties on list order.

    ISO 8601 strings sort lexicographically when they share an offset, which
    is what ``now_iso`` always emits. A record missing ``started_at``
    doesn't reach here (:func:`_parse_line` rejects it), so comparison is
    safe. Preserves insertion order among ties so the last-appended wins.
    """
    if not runs:
        return None
    best = runs[0]
    for r in runs[1:]:
        if r.started_at >= best.started_at:
            best = r
    return best


@dataclass
class StageAggregate:
    """Public shape for one stage in :func:`aggregate_timeline` output.

    The dot's visual state is driven by :attr:`status`; the tooltip is
    driven by :attr:`latest`. :attr:`count` is a cheap "how active is
    this stage" hint for the UI to show a badge once we need one.
    """

    stage: str
    status: str
    latest: StageRun | None = None
    count: int = 0
    runs: list[StageRun] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "stage": self.stage,
            "status": self.status,
            "count": self.count,
            "latest": self.latest.to_json() if self.latest else None,
            "runs": [r.to_json() for r in self.runs],
        }


def aggregate_timeline(status_dir: Path, item_slug: str) -> dict:
    """Return the 5-stage timeline rollup for one item.

    Always returns exactly five entries in :data:`STAGES` order so the
    frontend renders five dots even when the item has no history. The
    envelope carries ``slug`` (normalised) so the client can confirm it
    looked up the right record.
    """
    slug = safe_slug(item_slug)
    runs = read_stage_runs(status_dir, item_slug=slug) if slug else []
    collapsed = _collapse_tasked_runs(runs)
    by_stage: dict[str, list[StageRun]] = {s: [] for s in STAGES}
    for r in collapsed:
        by_stage[r.stage].append(r)
    stages: list[dict] = []
    for name in STAGES:
        bucket = by_stage[name]
        agg = StageAggregate(
            stage=name,
            status=_rollup_status(bucket),
            latest=_latest(bucket),
            count=len(bucket),
            runs=bucket,
        )
        stages.append(agg.to_json())
    return {"slug": slug, "stages": stages}
