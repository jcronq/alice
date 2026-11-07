"""Active scheduled jobs panel (issue #810, Phase 1).

Renders Speaking's active cron jobs — one row per scheduled job with a
human-readable schedule summary, countdown to next fire, and last-run
status. The panel is read-only in Phase 1; cancel / inspect interactions
and durable crons (watcher-owned from #782) land in Phase 2.

Data source
-----------

A JSON file at ``~/alice-mind/inner/state/active-crons.json`` kept in
sync by Speaking (the writer path lives in
:mod:`alice_speaking.schedule_state`). The file is a single object::

    {
      "updated_at": "2026-11-07T15:30:00-05:00",
      "jobs": [
        {
          "id": "job-abc123",
          "cron": "*/5 * * * *",
          "description": "babysit PRs",
          "created_at": "2026-11-07T14:00:00-05:00",
          "last_run_at": "2026-11-07T15:25:00-05:00",
          "last_run_outcome": "ok",   // ok | error | none
          "next_run_at": "2026-11-07T15:30:00-05:00"
        }
      ]
    }

All fields except ``id`` and ``cron`` are optional; the renderer degrades
gracefully when any are missing.

Security
--------

Job prompts live only in Speaking's process memory; they MUST NOT appear
in the dumped JSON. The frontend has no way to render them even if they
did, but defense-in-depth: this module filters any ``prompt`` /
``prompt_preview`` keys on read so a future writer bug can't leak.

Rendering is cheap (one JSON read + a tiny cron humanizer) so this
source is called inline per render tick — no background poller.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from status_site.sources.common import staleness_footer

SOURCE_NAME = "schedules"

#: Keys we never surface — Speaking's cron prompts may contain internal
#: notes, model-only directives, or credential-like text. Stripped on
#: read regardless of what the writer emits.
FORBIDDEN_FIELDS: frozenset[str] = frozenset({"prompt", "prompt_preview"})


def active_crons_path(env: dict[str, str] | None = None) -> Path:
    """Resolve ``~/alice-mind/inner/state/active-crons.json``.

    Overridable via ``ACTIVE_CRONS_PATH`` (full path) or
    ``ALICE_MIND`` (base dir). Matches the convention used by the
    task source and other inner-state readers.
    """
    e = dict(os.environ if env is None else env)
    override = e.get("ACTIVE_CRONS_PATH")
    if override:
        return Path(override)
    mind = e.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    return Path(mind) / "inner" / "state" / "active-crons.json"


# ─── cron parsing / humanising ──────────────────────────────────────


def _parse_iso(value: object) -> float | None:
    """ISO-8601 (``Z`` or offset) → epoch seconds; ``None`` on junk."""
    s = str(value or "").strip().strip('"').strip("'")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


#: Common names so we can say "every Monday" instead of "every 1 of week".
_WEEKDAYS = {
    "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
    "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
}
_MONTHS = {
    "1": "Jan", "2": "Feb", "3": "Mar", "4": "Apr",
    "5": "May", "6": "Jun", "7": "Jul", "8": "Aug",
    "9": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}


def humanize_cron(expr: str) -> str:
    """Best-effort human-readable summary of a 5-field cron expression.

    Covers the shapes Alice actually schedules: ``*/N * * * *`` (every
    N min), ``0 * * * *`` (hourly), ``MM HH * * *`` (daily at HH:MM),
    ``MM HH * * D`` (weekly on day D at HH:MM), and so on. Falls back
    to the raw expression when the shape isn't one we recognize — the
    frontend will show it verbatim rather than lie.

    6-field (seconds-leading) and 7-field (year-trailing) exprs are
    normalised by trimming the extra parts so we parse the canonical
    5-field minute/hour/dom/month/dow positions.
    """
    raw = (expr or "").strip()
    if not raw:
        return ""
    parts = raw.split()
    # Normalise seconds-leading (6 fields) / year-trailing (6 or 7) to
    # the canonical 5-field shape. Heuristic: a bare 4-digit integer
    # at the end is a year suffix; otherwise 6-field is seconds-leading
    # (far more common in SDK-style crons).
    if len(parts) == 7:
        parts = parts[1:6]
    elif len(parts) == 6:
        if parts[-1].isdigit() and len(parts[-1]) == 4:
            parts = parts[:5]
        else:
            parts = parts[1:]
    if len(parts) != 5:
        return raw
    minute, hour, dom, month, dow = parts

    # ``*/N * * * *`` — every N minutes.
    if (
        minute.startswith("*/")
        and minute[2:].isdigit()
        and hour == "*" and dom == "*" and month == "*" and dow == "*"
    ):
        n = int(minute[2:])
        if n == 1:
            return "every minute"
        return f"every {n} min"

    # ``0 */N * * *`` — every N hours on the hour.
    if (
        minute.isdigit() and minute == "0"
        and hour.startswith("*/") and hour[2:].isdigit()
        and dom == "*" and month == "*" and dow == "*"
    ):
        n = int(hour[2:])
        if n == 1:
            return "hourly"
        return f"every {n}h"

    # ``MM 0 * * *`` — hourly at :MM.
    if (
        minute.isdigit() and hour == "*"
        and dom == "*" and month == "*" and dow == "*"
    ):
        mm = int(minute)
        if mm == 0:
            return "hourly"
        return f"hourly at :{mm:02d}"

    # ``MM HH * * *`` — daily at HH:MM.
    if (
        minute.isdigit() and hour.isdigit()
        and dom == "*" and month == "*" and dow == "*"
    ):
        return f"daily at {int(hour):02d}:{int(minute):02d}"

    # ``MM HH * * D`` — weekly on day D at HH:MM.
    if (
        minute.isdigit() and hour.isdigit()
        and dom == "*" and month == "*"
        and dow != "*"
    ):
        day_tokens = [t for t in dow.split(",") if t]
        names = [_WEEKDAYS.get(t.strip(), t.strip()) for t in day_tokens]
        if len(names) == 1:
            return f"weekly on {names[0]} at {int(hour):02d}:{int(minute):02d}"
        return f"{','.join(names)} at {int(hour):02d}:{int(minute):02d}"

    # ``MM HH D * *`` — monthly on the Dth at HH:MM.
    if (
        minute.isdigit() and hour.isdigit()
        and dom.isdigit() and month == "*" and dow == "*"
    ):
        return f"monthly on day {int(dom)} at {int(hour):02d}:{int(minute):02d}"

    return raw


def _fmt_countdown(seconds: float) -> str:
    """``in 45s`` / ``in 12m`` / ``in 3h`` / ``in 2d``; past → ``overdue``."""
    s = int(seconds)
    if s <= 0:
        return "overdue"
    if s < 60:
        return f"in {s}s"
    m = s // 60
    if m < 60:
        return f"in {m}m"
    h = m // 60
    if h < 24:
        return f"in {h}h"
    return f"in {h // 24}d"


def _fmt_ago(seconds: float) -> str:
    """``45s ago`` / ``12m ago`` / ``3h ago`` / ``2d ago``; future → ``just now``."""
    s = int(seconds)
    if s <= 0:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    m = s // 60
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


# ─── reader / typed-row builder ─────────────────────────────────────


def read_jobs(
    path: Path, *, now: float | None = None
) -> list[dict]:
    """Read ``active-crons.json`` → sanitised, typed rows.

    Missing file → ``[]``. Malformed JSON → ``[]`` (logged by caller
    via the normal "missing poll" path; this source stays quiet so a
    broken writer can't flood logs).

    Each returned row carries only safe fields: ``id``, ``cron``,
    ``schedule_summary`` (humanised), ``description``, ``created_at``,
    ``last_run_at``, ``last_run_outcome``, ``next_run_at``,
    ``next_run_seconds`` (countdown from ``now`` — negative when
    overdue), ``next_run_label`` (human countdown), ``last_run_label``
    (human "ago"). ``FORBIDDEN_FIELDS`` (prompt / prompt_preview) are
    always stripped.

    Rows are sorted by ``next_run_at`` ascending (soonest first), with
    unknown-next-run rows placed at the end for stable diffing.
    """
    t_now = now if now is not None else datetime.now(tz=timezone.utc).timestamp()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list):
        return []
    out: list[dict] = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        clean = {k: v for k, v in job.items() if k not in FORBIDDEN_FIELDS}
        job_id = str(clean.get("id") or "").strip()
        cron = str(clean.get("cron") or "").strip()
        if not job_id:
            continue
        next_at = _parse_iso(clean.get("next_run_at"))
        last_at = _parse_iso(clean.get("last_run_at"))
        row = {
            "id": job_id,
            "cron": cron,
            "schedule_summary": humanize_cron(cron),
            "description": str(clean.get("description") or "").strip(),
            "created_at": str(clean.get("created_at") or ""),
            "last_run_at": str(clean.get("last_run_at") or ""),
            "last_run_outcome": str(clean.get("last_run_outcome") or ""),
            "next_run_at": str(clean.get("next_run_at") or ""),
            "next_run_seconds": (
                next_at - t_now if next_at is not None else None
            ),
            "next_run_label": (
                _fmt_countdown(next_at - t_now) if next_at is not None else ""
            ),
            "last_run_label": (
                _fmt_ago(t_now - last_at) if last_at is not None else ""
            ),
        }
        out.append(row)
    out.sort(
        key=lambda r: (
            0 if r["next_run_seconds"] is not None else 1,
            r["next_run_seconds"] if r["next_run_seconds"] is not None else 0.0,
            r["id"],
        )
    )
    return out


def typed_rows(jobs: Iterable[dict]) -> list[dict]:
    """Shape the SSE payload sees. Identical to :func:`read_jobs` output
    today; kept as a separate seam so the panel's typed contract can
    diverge from the on-disk JSON without breaking the writer."""
    return [dict(j) for j in jobs]


def render(jobs: Sequence[dict], *, now: datetime | None = None) -> str:
    """Compact markdown fallback for ``/index.md`` consumers.

    The reactive shell renders the typed rows directly in the
    ``SchedulesPanel`` widget; this fallback keeps the markdown page
    serveable for curl-tier clients and matches the staleness-footer
    convention every other source follows.
    """
    if not jobs:
        body = "*(no scheduled jobs)*"
    else:
        lines: list[str] = []
        for j in jobs:
            sched = j.get("schedule_summary") or j.get("cron") or ""
            desc = str(j.get("description") or "").strip()
            label = str(j.get("next_run_label") or "")
            outcome = str(j.get("last_run_outcome") or "")
            parts = [f"- **{j.get('id')}** — {sched}"]
            if desc:
                parts.append(f"— {desc}")
            tail_bits = []
            if label:
                tail_bits.append(label)
            if outcome:
                tail_bits.append(f"last: {outcome}")
            if tail_bits:
                parts.append("_(" + " · ".join(tail_bits) + ")_")
            lines.append(" ".join(parts))
        body = "\n".join(lines)
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)
