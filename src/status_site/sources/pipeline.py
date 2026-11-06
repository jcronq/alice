"""Pipeline board — the whole SM pipeline in one place (issue #653).

Alice's queue used to read only ``inner/surface/*.md``. The SM v2
pipeline on GitHub issues is the primary tracking system, so this
source unions three categories of pending work:

1. **SM-active GH issues** — open issues carrying one of
   :data:`PIPELINE_STATES` and none of :data:`EXCLUDED_LABELS`. Fetched
   by :func:`fetch_issues` (one ``gh issue list`` per repo; a failing
   repo is logged and skipped, never fatal).
2. **Surfaces** — unchanged; scanned inline by the server.
3. **Task store** — ``inner/tasks/task-*/task.yaml`` whose status isn't
   terminal; scanned by :func:`scan_tasks`.

The ``gh`` call is the only slow part; the server's background poller
caches :func:`fetch_issues` output on a ~30s tick and the renderer
reads the cache.

Age caveat: GitHub doesn't expose "time of last label change" without a
per-issue timeline call, so the issue ``age`` is time since
``updatedAt`` — a proxy that also moves on comments.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from alice_forge.review_queue_renderer import _coerce_repos, _load_config, gh

log = logging.getLogger("status_site.pipeline")

SOURCE_NAME = "gh-sm-pipeline"

DEFAULT_REPOS_SM = ["jcronq/alice"]

#: Pipeline order (also the display group order).
PIPELINE_STATES: tuple[str, ...] = (
    "selected",
    "designing",
    "design_review",
    "designed",
    "building",
    "reviewing",
    "blocked",
)
_STATE_RANK = {s: i for i, s in enumerate(PIPELINE_STATES)}

#: An issue carrying any of these is out of the active pipeline even if
#: a stale intermediate label is still attached. ``sm:blocked`` is
#: deliberately NOT excluded — Jason wants blocked work visible so
#: dispatcher failures (bug #342) don't vanish from the queue.
EXCLUDED_LABELS: frozenset[str] = frozenset(
    {
        "sm:draft",
        "sm:done",
        "sm:closed",
        "sm:rejected",
    }
)

TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"done", "closed", "rejected"}
)

SubprocessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def repos_sm_from_config(cfg_loader=_load_config) -> list[str]:
    """Repos to scan. Same config block as ``repos_pr``; key ``repos_sm``."""
    return _coerce_repos(cfg_loader().get("repos_sm"), DEFAULT_REPOS_SM)


def _parse_iso(value: object) -> float | None:
    """ISO-8601 (``Z`` or offset) → epoch seconds; ``None`` on junk."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _label_names(issue: dict) -> set[str]:
    out: set[str] = set()
    for entry in issue.get("labels") or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str):
            out.add(name)
    return out


def classify_issue(issue: dict) -> tuple[str, str] | None:
    """Return ``(state, art)`` for an active pipeline issue, else ``None``.

    ``state`` is the sm label without its prefix. If several pipeline
    labels are present the most advanced one wins. ``art`` is the
    ``art:*`` suffix or ``""``.
    """
    labels = _label_names(issue)
    if labels & EXCLUDED_LABELS:
        return None
    states = [
        lbl[len("sm:"):]
        for lbl in labels
        if lbl.startswith("sm:") and lbl[len("sm:"):] in _STATE_RANK
    ]
    if not states:
        return None
    state = max(states, key=_STATE_RANK.__getitem__)
    art = next((l[len("art:"):] for l in sorted(labels) if l.startswith("art:")), "")
    return state, art


def fetch_issues(
    repos: Sequence[str],
    *,
    runner: SubprocessRunner | None = None,
) -> tuple[list[dict], list[str]]:
    """Fetch SM-active issues across ``repos``.

    Returns ``(rows, failed_repos)``. Rows are sorted by pipeline state
    then issue number. A repo whose ``gh`` call fails (or returns
    something unparseable) is logged, added to ``failed_repos``, and
    skipped.
    """
    rows: list[dict] = []
    failed: list[str] = []
    for repo in repos:
        try:
            raw = gh(
                [
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    "200",
                    "--json",
                    "number,title,url,labels,updatedAt",
                ],
                runner=runner,
            )
            for issue in raw:
                cls = classify_issue(issue)
                if cls is None:
                    continue
                state, art = cls
                rows.append(
                    {
                        "repo": repo,
                        "number": int(issue["number"]),
                        "title": str(issue.get("title") or ""),
                        "url": str(issue.get("url") or ""),
                        "state": state,
                        "art": art,
                        "updated_epoch": _parse_iso(issue.get("updatedAt")),
                    }
                )
        except Exception as e:  # noqa: BLE001 — one repo must not poison the rest
            log.warning("pipeline: gh query failed for %s: %r", repo, e)
            failed.append(repo)
    rows.sort(key=lambda r: (_STATE_RANK[r["state"]], r["repo"], r["number"]))
    return rows, failed


def issue_rows(cached: list[dict], *, now: float) -> list[dict]:
    """Cached issues → widget rows with ``age_seconds`` (``None`` if unknown)."""
    out: list[dict] = []
    for r in cached:
        ts = r.get("updated_epoch")
        out.append(
            {
                "repo": r.get("repo", ""),
                "number": r.get("number"),
                "title": r.get("title", ""),
                "state": r.get("state", ""),
                "art": r.get("art", ""),
                "link": r.get("url", ""),
                "age_seconds": max(0.0, now - ts) if ts else None,
            }
        )
    return out


def _read_task_yaml(text: str) -> dict[str, str]:
    """Top-level ``key: value`` scalars only; enough for status/title/updated."""
    data: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0] in " \t#-" or ":" not in line:
            continue
        k, _, v = line.partition(":")
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def scan_tasks(tasks_dir: Path, *, now: float) -> list[dict]:
    """Non-terminal tasks under ``tasks_dir/task-*/task.yaml``.

    Row shape: ``{slug, title, state, age_seconds, link}``. ``link`` is
    always ``""`` — there is no task page to point at yet. Age comes
    from the ``updated`` field, falling back to the file's mtime when
    frontmatter is missing or unparseable. Unreadable tasks are skipped.
    Sorted oldest-first.
    """
    out: list[dict] = []
    if not tasks_dir.is_dir():
        return out
    for d in sorted(tasks_dir.glob("task-*")):
        f = d / "task.yaml"
        try:
            text = f.read_text(errors="replace")
            mtime = f.stat().st_mtime
        except OSError:
            continue
        fm = _read_task_yaml(text)
        status = fm.get("status", "").lower()
        if status in TERMINAL_TASK_STATUSES:
            continue
        ts = _parse_iso(fm.get("updated")) or _parse_iso(fm.get("created")) or mtime
        out.append(
            {
                "slug": fm.get("id") or d.name,
                "title": fm.get("title", ""),
                "state": status or "unknown",
                "age_seconds": max(0.0, now - ts),
                "link": "",
            }
        )
    out.sort(key=lambda r: -r["age_seconds"])
    return out


def render_markdown(
    issues: Sequence[dict], surfaces: Sequence[dict], tasks: Sequence[dict]
) -> str:
    """Minimal fallback — the detail lives in the typed rows."""
    if not (issues or surfaces or tasks):
        return "*(queue clear)*"
    parts = [f"{len(issues)} items in pipeline"]
    if surfaces:
        parts.append(f"{len(surfaces)} surfaces")
    if tasks:
        parts.append(f"{len(tasks)} tasks")
    return ", ".join(parts)
