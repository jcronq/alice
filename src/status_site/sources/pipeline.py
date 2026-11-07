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

Review-note synthesis (issue #719): for every open issue carrying both
``sm:design_review`` AND ``art:code``, :func:`ensure_review_notes`
drops a ``review-ghi-<repo>-<num>.md`` into ``STATUS_DIR/notes`` so
the status-site Review page has something to render. When an issue
moves off ``sm:design_review`` the stale note is removed. The notes
are idempotent against ``updatedAt`` — same stamp → no rewrite.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from alice_forge.review_queue_renderer import _coerce_repos, _load_config, gh

log = logging.getLogger("status_site.pipeline")

SOURCE_NAME = "gh-sm-pipeline"

DEFAULT_REPOS_SM = ["jcronq/alice"]

#: Pipeline order (also the display group order).
PIPELINE_STATES: tuple[str, ...] = (
    "draft",
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
    art = next((l[len("art:"):] for l in sorted(labels) if l.startswith("art:")), "")
    if not states:
        # Untriaged (issue #695): explicit sm:draft, or no sm:* label at
        # all but an art:* label (an intentionally-filed work item).
        has_sm = any(l.startswith("sm:") for l in labels)
        if "sm:draft" in labels or (not has_sm and art):
            return "draft", art
        return None
    state = max(states, key=_STATE_RANK.__getitem__)
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

    Each row carries ``body`` (issue body markdown, possibly empty) and
    ``updated_at`` (raw ISO-8601 from ``updatedAt``) so the review-note
    synthesis step (:func:`ensure_review_notes`) can run off the same
    fetch without re-querying.
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
                    "number,title,url,labels,updatedAt,body",
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
                        "updated_at": str(issue.get("updatedAt") or ""),
                        "body": str(issue.get("body") or ""),
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


#: Issue states whose next action belongs to Jason (issue #691). Every
#: other pipeline state — including ``blocked``, which stays visible but
#: is the dispatcher's to unstick — is Alice-driven.
JASON_STATES: frozenset[str] = frozenset({"design_review"})


def owner_of(state: str) -> str:
    """``"jason"`` or ``"alice"`` — who owns the next action for ``state``."""
    return "jason" if state in JASON_STATES else "alice"


def sort_rows(rows: Sequence[dict]) -> list[dict]:
    """Oldest first (unknown age last), then pipeline state, repo, number.

    Pipeline issues carry no urgency, so age is the only ordering signal
    inside a block; ask rows are urgency-sorted separately.
    """

    def key(r: dict):
        age = r.get("age_seconds")
        return (
            0 if age is not None else 1,
            -(age or 0.0),
            _STATE_RANK.get(r.get("state", ""), len(_STATE_RANK)),
            r.get("repo", ""),
            r.get("number") or 0,
        )

    return sorted(rows, key=key)


def split_by_owner(issues: Sequence[dict]) -> dict[str, list[dict]]:
    """Route SM-active issue rows into ``{"jason": [...], "alice": [...]}``.

    ``design_review`` → Jason; ``selected`` / ``designing`` / ``designed`` /
    ``building`` / ``reviewing`` / ``blocked`` (and anything unrecognised)
    → Alice. Each list is :func:`sort_rows`-ordered.
    """
    buckets: dict[str, list[dict]] = {"jason": [], "alice": []}
    for r in issues:
        buckets[owner_of(str(r.get("state") or ""))].append(r)
    return {k: sort_rows(v) for k, v in buckets.items()}


def age_label_seconds(label: object) -> float | None:
    """Approximate seconds for review-queue age labels (``<1h`` / ``5h`` / ``3d``)."""
    s = str(label or "").strip()
    if s == "<1h":
        return 1800.0
    if len(s) > 1 and s[:-1].isdigit() and s[-1] in "hd":
        return int(s[:-1]) * (3600.0 if s[-1] == "h" else 86400.0)
    return None


def review_item_row(item: object, *, state: str = "") -> dict:
    """A review-queue ``DesignItem`` / ``PRItem`` (or dict) → widget row."""
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    return {
        "repo": get("repo", "") or "",
        "number": get("number"),
        "title": get("title", "") or "",
        "state": state,
        "art": "",
        "link": get("url", "") or "",
        "age_seconds": age_label_seconds(get("age", "")),
        "author": get("author", "") or "",
        "ci": get("ci", "") or "",
    }


def merge_design_review(
    pipeline_rows: Sequence[dict], review_items: Sequence[object]
) -> list[dict]:
    """Union of ``sm:design_review`` pipeline rows and review-queue items.

    The two sources overlap (both are label queries); pipeline rows win
    on ``(repo, number)`` because their age is exact.
    """
    out = list(pipeline_rows)
    seen = {(r.get("repo"), r.get("number")) for r in out}
    for item in review_items:
        row = review_item_row(item, state="design_review")
        if (row["repo"], row["number"]) not in seen:
            out.append(row)
            seen.add((row["repo"], row["number"]))
    return sort_rows(out)


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


# ─── review-note synthesis (issue #719) ─────────────────────────────


#: Prefix for review notes auto-emitted from GH issues. The status-site
#: Review page keys on the full file stem (``review-<slug>.md``) — this
#: constant is the ``<slug>`` portion's own prefix, used both when
#: writing new notes and when sweeping stale ones.
REVIEW_NOTE_PREFIX = "ghi-"


def review_slug_for(repo: str, number: int | str) -> str:
    """Stable slug for a GH-issue-backed review note.

    ``jcronq/alice`` + ``704`` → ``ghi-jcronq-alice-704``. Lowercased
    and sanitised to match :func:`status_site.server._review_slug`.
    """
    safe_repo = str(repo or "").lower().replace("/", "-")
    safe_repo = "".join(c for c in safe_repo if c.isalnum() or c == "-")
    return f"{REVIEW_NOTE_PREFIX}{safe_repo}-{int(number)}"


def _render_review_note(row: dict) -> str:
    """Build the full markdown text for one review note."""
    title = str(row.get("title") or "").replace('"', "'")
    repo = str(row.get("repo") or "")
    number = row.get("number") or ""
    url = str(row.get("url") or "")
    updated_at = str(row.get("updated_at") or "")
    body = str(row.get("body") or "").strip()
    slug = review_slug_for(repo, number) if repo and number else ""
    if not body:
        body = "_(no description)_"
    fm_lines = [
        "---",
        "kind: decide",
        f"slug: {slug}",
        f'title: "{title}"',
        f"repo: {repo}",
        f"issue: {number}",
        f"url: {url}",
        f"updated: {updated_at}",
        "---",
        "",
        body,
        "",
    ]
    return "\n".join(fm_lines)


def _parse_review_frontmatter(text: str) -> dict[str, str]:
    """Shallow YAML-ish parse of a review note's frontmatter.

    Mirrors :func:`status_site.server.parse_frontmatter` without the
    dependency. Only cares about the top-level scalar keys this module
    writes; comments and nested structures are not expected.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line[0] in " \t#-" or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_atomic(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` via a tmp file + rename (atomic on POSIX)."""
    tmp = path.with_name(f".tmp-{path.name}-{os.getpid()}-{time.time_ns()}")
    tmp.write_text(body)
    os.replace(tmp, path)


def ensure_review_notes(
    rows: Sequence[dict], notes_dir: Path
) -> dict[str, list[str]]:
    """Reconcile ``notes_dir/review-ghi-*.md`` against the live row set.

    For every row with ``state == "design_review"`` AND ``art ==
    "code"`` AND a non-empty ``repo`` + ``number``: ensure a note file
    exists with an up-to-date ``updated:`` frontmatter. If the file's
    stored ``updated`` matches the row's ``updated_at`` the file is
    left alone (idempotent).

    Any pre-existing ``review-ghi-*.md`` whose slug is NOT in the live
    set is deleted — the issue has moved off ``sm:design_review`` (or
    off ``art:code``, or has been closed) and the note is stale.

    Returns ``{"written": [slug, ...], "deleted": [slug, ...]}`` for
    logging. The caller handles exceptions — a single bad row must not
    stop the sweep.
    """
    written: list[str] = []
    deleted: list[str] = []
    live_slugs: set[str] = set()

    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("pipeline: cannot create notes_dir %s: %r", notes_dir, e)
        return {"written": written, "deleted": deleted}

    for row in rows:
        if row.get("state") != "design_review" or row.get("art") != "code":
            continue
        repo = str(row.get("repo") or "")
        number = row.get("number")
        if not repo or not number:
            continue
        slug = review_slug_for(repo, number)
        live_slugs.add(slug)
        # File stem == slug. ``read_review`` opens ``notes/<slug>.md``
        # (no "review-" filename prefix); the ``ghi-`` prefix inside
        # the slug is the namespace that keeps us from colliding with
        # existing hand-written notes.
        path = notes_dir / f"{slug}.md"
        desired_updated = str(row.get("updated_at") or "")
        try:
            existing = path.read_text(errors="replace") if path.exists() else ""
        except OSError as e:
            log.warning("pipeline: cannot read %s: %r", path, e)
            existing = ""
        if existing:
            fm = _parse_review_frontmatter(existing)
            if fm.get("updated") == desired_updated and fm.get("title") == str(
                row.get("title") or ""
            ).replace('"', "'"):
                continue
        try:
            _write_atomic(path, _render_review_note(row))
            written.append(slug)
        except OSError as e:
            log.warning("pipeline: cannot write %s: %r", path, e)

    # Sweep stale notes that match our ownership prefix.
    try:
        for p in notes_dir.glob(f"{REVIEW_NOTE_PREFIX}*.md"):
            slug = p.stem
            if slug in live_slugs:
                continue
            try:
                p.unlink()
                deleted.append(slug)
            except OSError as e:
                log.warning("pipeline: cannot delete stale %s: %r", p, e)
    except OSError as e:
        log.warning("pipeline: cannot scan %s: %r", notes_dir, e)

    return {"written": written, "deleted": deleted}
