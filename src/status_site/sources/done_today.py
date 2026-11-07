"""Done today — merged PRs plus resolved asks, keyed on today's
local date.

Two sources, both tolerant of partial failure:

1. ``gh pr list --search "is:merged merged:>=<today>"`` across watched
   repos (same config key as #622 — ``review_queue_renderer.repos_pr``
   is reused so Jason has one list of repos to maintain).
2. ``asks.jsonl`` resolved records whose ``resolved_at`` lands on the
   local day.

Day-roll archive: once per local date boundary the server's background
poller calls :func:`roll_day` which appends the previous day's items
to ``done-archive.md`` and bumps a sentinel file so restarts don't
double-append.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from alice_forge.review_queue_renderer import (
    DEFAULT_REPOS_PR,
    _coerce_repos,
    _load_config,
    age_str,
    gh,
)
from status_site.sources import db_readers as _db_readers
from status_site.sources.common import staleness_footer

SOURCE_NAME = "gh-merged-prs"

SubprocessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class MergedPR:
    repo: str
    number: int
    title: str
    url: str
    merged_at: str  # ISO8601 from gh


def today_local(now: datetime | None = None) -> date:
    """Return today's local date.

    ``now`` is injectable so tests can pin the clock — the production
    call site passes ``datetime.now().astimezone()``.
    """
    return (now or datetime.now().astimezone()).date()


def fetch_merged_prs(
    repos: Iterable[str],
    *,
    today: date | None = None,
    runner: SubprocessRunner | None = None,
) -> tuple[list[MergedPR], list[str]]:
    """Fetch PRs merged on ``today`` (local date) across ``repos``.

    Returns ``(items, failed_repos)``. The ``gh`` query asks for
    ``merged:>=<today>`` so GitHub's server-side filter does the
    heavy lifting; we then re-filter by ``merged_at.date() == today``
    to catch the UTC-vs-local-date boundary (a merge at 23:58 local
    time could have been ``merged:>=today`` in UTC but belong to
    tomorrow once converted back, or vice versa).

    Source selection: the ``STATUS_SITE_READ_FROM`` env flag picks
    between ``db`` (default) and ``gh``. Explicit ``runner`` wiring
    still forces the subprocess path so existing tests keep working.
    """
    if runner is None and _db_readers.read_from() == _db_readers.MODE_DB:
        return _db_readers.fetch_merged_prs_from_db(repos, today=today)

    if today is None:
        today = today_local()
    items: list[MergedPR] = []
    failed: list[str] = []
    date_str = today.isoformat()
    for repo in repos:
        try:
            raw = gh(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "merged",
                    "--search",
                    f"merged:>={date_str}",
                    "--json",
                    "number,title,url,mergedAt",
                ],
                runner=runner,
            )
        except RuntimeError:
            failed.append(repo)
            continue
        for row in raw:
            merged_at = str(row.get("mergedAt") or "").strip()
            if not merged_at:
                continue
            try:
                when = datetime.fromisoformat(
                    merged_at.replace("Z", "+00:00")
                ).astimezone()
            except ValueError:
                continue
            if when.date() != today:
                continue
            items.append(
                MergedPR(
                    repo=repo,
                    number=int(row["number"]),
                    title=str(row.get("title", "")).strip(),
                    url=str(row.get("url", "")),
                    merged_at=merged_at,
                )
            )
    return items, failed


def resolved_today(records: Sequence[dict], today: date) -> list[dict]:
    """Return ask records resolved on ``today`` (local date), newest
    first. Malformed or missing ``resolved_at`` → excluded (we can
    only archive what we have a timestamp for).
    """
    out: list[dict] = []
    for r in records:
        if not r.get("resolved"):
            continue
        ts = r.get("resolved_at")
        if not isinstance(ts, str):
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if when.date() != today:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("resolved_at") or "", reverse=True)
    return out


def _render_pr_line(pr: MergedPR) -> str:
    return (
        f"- [{pr.repo}#{pr.number}]({pr.url}) — {pr.title} — "
        f"merged {age_str(pr.merged_at)} ago"
    )


def _render_ask_line(ask: dict) -> str:
    text = str(ask.get("text") or "").strip() or "(unnamed ask)"
    return f"- [x] {text}"


def render(
    prs: Sequence[MergedPR],
    asks: Sequence[dict],
    *,
    now: datetime | None = None,
) -> str:
    """Render the Done-today region body (no heading).

    Two sub-lists under ``### PRs merged today`` and ``### Asks
    resolved today``, each with an ``(none)`` empty state so the shape
    is stable even on a quiet day. Trailing staleness footer always
    present.
    """
    lines: list[str] = []
    lines.append("### PRs merged today")
    lines.append("")
    if prs:
        lines.extend(_render_pr_line(p) for p in prs)
    else:
        lines.append("*(none)*")
    lines.append("")
    lines.append("### Asks resolved today")
    lines.append("")
    if asks:
        lines.extend(_render_ask_line(a) for a in asks)
    else:
        lines.append("*(none)*")
    lines.append("")
    body = "\n".join(lines).rstrip()
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)


# ─── Day-roll archive ───────────────────────────────────────────────


DAY_ROLL_SENTINEL_NAME = ".done-archive-last-roll"


def _load_sentinel(sentinel_path: Path) -> date | None:
    try:
        text = sentinel_path.read_text().strip()
    except OSError:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _write_sentinel(sentinel_path: Path, when: date) -> None:
    sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sentinel_path.with_suffix(sentinel_path.suffix + ".tmp")
    tmp.write_text(when.isoformat())
    tmp.replace(sentinel_path)


def roll_day(
    status_dir: Path,
    *,
    today: date | None = None,
    load_prs: Callable[[date], Sequence[MergedPR]] = lambda d: (),
    load_asks: Callable[[date], Sequence[dict]] = lambda d: (),
) -> bool:
    """If the sentinel's last-rolled date is strictly before ``today``,
    append the previous day's content to ``done-archive.md`` and bump
    the sentinel. Returns True iff an archive write happened.

    The archive append is best-effort: once-per-day semantics come
    from the sentinel comparison, not from reading the archive file
    itself (so a user editing the archive by hand doesn't retrigger
    the write). Content for the previous day is fetched through the
    injectable ``load_prs`` / ``load_asks`` closures so the server
    can reuse its cached results.
    """
    if today is None:
        today = today_local()
    sentinel = status_dir / DAY_ROLL_SENTINEL_NAME
    last = _load_sentinel(sentinel)
    if last is not None and last >= today:
        return False
    archive_path = status_dir / "done-archive.md"
    status_dir.mkdir(parents=True, exist_ok=True)
    yesterday = date.fromordinal(today.toordinal() - 1) if last is None else last
    # When sentinel is uninitialised, roll yesterday's entries so the
    # first call seeds a boundary without rewinding history.
    prs = list(load_prs(yesterday))
    asks = list(load_asks(yesterday))
    if prs or asks:
        with open(archive_path, "a") as f:
            f.write(f"\n## {yesterday.isoformat()}\n\n")
            for p in prs:
                f.write(_render_pr_line(p) + "\n")
            for a in asks:
                f.write(_render_ask_line(a) + "\n")
    _write_sentinel(sentinel, today)
    return True


def repos_pr_from_config(cfg_loader=_load_config) -> list[str]:
    """Reuse #622's config key for the repo list."""
    cfg = cfg_loader()
    return _coerce_repos(cfg.get("repos_pr"), DEFAULT_REPOS_PR)


def _dumps(obj: object) -> str:
    """json.dumps alias — makes it cheap to swap in a canonical
    encoder later if we need stable archive diffs."""
    return json.dumps(obj, sort_keys=True)
