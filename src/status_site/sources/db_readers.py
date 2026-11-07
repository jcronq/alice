"""DB-backed readers for the status-site pipeline sources (#790 Step 3).

Each public function matches the shape of its GH-backed counterpart so
the three source modules can switch implementations behind a flag. The
flag is :func:`read_from` — ``"db"`` (default) pulls from
``alice-state.db``; ``"gh"`` is the pre-#790 path and remains the
rollback.

Scope boundaries:

* **No GH calls here.** The poller
  (:mod:`alice_state_db.gh_poll`) is the only path that touches
  GitHub; these readers just ``SELECT`` from the local SQLite file.
* **No writes.** Opened read-only (via :func:`alice_state_db.connect`),
  so even if a reader races the memory-worker's poll it can't
  corrupt state.
* **Fallback-safe.** When the DB file is missing or empty, every
  helper returns an empty list / zero rows rather than raising — so
  the server can degrade gracefully during the first poll cycle on a
  fresh deploy.

The three reader entry points mirror the GH-side fetchers:

* :func:`fetch_issues_from_db` → shape of
  :func:`status_site.sources.pipeline.fetch_issues` rows.
* :func:`fetch_design_review_from_db` + :func:`fetch_pr_review_from_db`
  → shapes of :func:`alice_forge.review_queue_renderer.fetch_design_review_issues`
  and :func:`alice_forge.review_queue_renderer.fetch_pr_review`.
* :func:`fetch_merged_prs_from_db` → shape of
  :func:`status_site.sources.done_today.fetch_merged_prs`.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from alice_state_db import DEFAULT_DB_PATH, connect

log = logging.getLogger("status_site.db_readers")


#: Environment flag name. ``db`` (default) reads from alice-state.db;
#: ``gh`` falls back to the pre-#790 ``gh`` subprocess path.
ENV_FLAG = "STATUS_SITE_READ_FROM"
MODE_DB = "db"
MODE_GH = "gh"


def read_from(default: str = MODE_DB) -> str:
    """Return the currently-active reader mode.

    Centralised so every source sees the same value on a single
    request — avoids a race where one source reads DB and another
    reads GH mid-flip. Unknown values are treated as the default so
    a typo doesn't silently take the Overview down.
    """
    raw = os.environ.get(ENV_FLAG, default)
    val = (raw or default).strip().lower()
    return val if val in (MODE_DB, MODE_GH) else default


def _resolve_db_path() -> pathlib.Path:
    """Resolve the DB path (env override or canonical default).

    The override (``ALICE_STATE_DB_PATH``) stays implementation-detail
    scope — tests use it, operators generally don't.
    """
    raw = os.environ.get("ALICE_STATE_DB_PATH")
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path(DEFAULT_DB_PATH)


# ---------------------------------------------------------------------------
# Shared helpers.


_PIPELINE_STATES = (
    "selected",
    "designing",
    "design_review",
    "designed",
    "building",
    "reviewing",
    "blocked",
)
_STATE_RANK = {s: i for i, s in enumerate(_PIPELINE_STATES)}


def _parse_iso_epoch(value: Optional[str]) -> Optional[float]:
    """ISO → epoch; mirrors ``pipeline._parse_iso`` behaviour."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _item_id_to_repo_number(item_id: str) -> Optional[tuple[str, int]]:
    """``gh:jcronq/alice#790`` → ``("jcronq/alice", 790)``.

    ``gh:pr:...`` entries return ``None`` because the parse target here
    is strictly ``gh:issue`` items. Returns ``None`` on malformed input
    so the caller can filter.
    """
    if not item_id.startswith("gh:") or ":pr:" in item_id:
        return None
    body = item_id[len("gh:") :]
    if "#" not in body:
        return None
    repo, _, num = body.partition("#")
    try:
        return repo, int(num)
    except ValueError:
        return None


def _pr_item_id_to_repo_number(item_id: str) -> Optional[tuple[str, int]]:
    """``gh:pr:jcronq/alice#802`` → ``("jcronq/alice", 802)``."""
    if not item_id.startswith("gh:pr:"):
        return None
    body = item_id[len("gh:pr:") :]
    if "#" not in body:
        return None
    repo, _, num = body.partition("#")
    try:
        return repo, int(num)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pipeline board readers.


def fetch_issues_from_db(
    repos: Iterable[str],
    *,
    db_path: Optional[pathlib.Path] = None,
) -> tuple[list[dict], list[str]]:
    """Return the pipeline-board row set from ``alice-state.db``.

    Row shape matches :func:`status_site.sources.pipeline.fetch_issues`
    so the pipeline source can swap this in transparently:

    .. code-block:: python

        {
          "repo": "jcronq/alice",
          "number": 790,
          "title": "...",
          "url": "https://github.com/jcronq/alice/issues/790",
          "state": "selected",
          "art": "code",  # label suffix if available, else ""
          "updated_epoch": 1731..,
          "updated_at": "2026-11-07T...",
          "body": "",     # not stored in DB; empty string
        }

    Second return value is ``failed_repos`` — always ``[]`` in the DB
    path because we read from a local file; the caller treats it the
    same as the GH path's empty list.
    """
    path = db_path or _resolve_db_path()
    repos_set = set(repos)
    rows: list[dict] = []

    # Build the SQL repo-filter clause from the item_id prefix so we
    # don't have to carry a separate ``repo`` column. ``item_id LIKE
    # 'gh:jcronq/alice#%'`` is cheap under the ``idx_items_type``
    # filter.
    try:
        with connect(path) as conn:
            cur = conn.execute(
                """
                SELECT
                    i.item_id,
                    i.title,
                    i.updated_at,
                    sm.state
                FROM items i
                JOIN state_machine sm ON sm.item_id = i.item_id
                WHERE i.item_type = 'gh:issue'
                ORDER BY i.updated_at DESC
                """
            )
            for row in cur.fetchall():
                parsed = _item_id_to_repo_number(row["item_id"])
                if parsed is None:
                    continue
                repo, number = parsed
                if repos_set and repo not in repos_set:
                    continue
                state_raw = (row["state"] or "").strip().lower()
                # DB stores either the bare state name ("selected") or
                # a ``sm:`` prefix that leaked in from the seed path.
                # Normalise.
                if state_raw.startswith("sm:"):
                    state_raw = state_raw[len("sm:") :]
                if state_raw not in _STATE_RANK:
                    continue
                updated_at = str(row["updated_at"] or "")
                rows.append(
                    {
                        "repo": repo,
                        "number": number,
                        "title": str(row["title"] or ""),
                        "url": f"https://github.com/{repo}/issues/{number}",
                        "state": state_raw,
                        "art": "",  # label suffixes not stored in Step 2
                        "updated_epoch": _parse_iso_epoch(updated_at),
                        "updated_at": updated_at,
                        "body": "",
                    }
                )
    except sqlite3.Error as exc:
        log.warning("db_readers.fetch_issues_from_db: %r", exc)
        return [], []

    # Issue #821: attach per-row ``components`` so the pipeline board
    # can group by ``src/`` bucket without a second query per render.
    if rows:
        comp_map = fetch_issue_components_from_db(
            [
                f"gh:{r['repo']}#{r['number']}" for r in rows
            ],
            db_path=path,
        )
        for r in rows:
            r["components"] = comp_map.get(
                f"gh:{r['repo']}#{r['number']}", []
            )

    rows.sort(key=lambda r: (_STATE_RANK[r["state"]], r["repo"], r["number"]))
    return rows, []


def fetch_issue_components_from_db(
    item_ids: Iterable[str],
    *,
    db_path: Optional[pathlib.Path] = None,
) -> dict[str, list[str]]:
    """Return ``{issue_item_id: [component, ...]}`` for issue #821.

    Resolution precedence (per item_id):

    1. PR-files rows (``ref_source LIKE 'pr-files:%'``) take precedence
       whenever any are present — they're the ground truth for what a
       PR actually touches, so the issue-label fallback is only used
       when no PR is linked yet.
    2. Issue-label rows (``ref_source='issue-label'``) are the fallback
       for pre-build items that carry a ``component:*`` label but
       don't yet have a PR.
    3. Neither → empty list. The caller buckets as ``pre-build``.

    One batched SELECT, grouping in Python — a single cold round-trip
    for the whole pipeline page.
    """
    ids = [i for i in item_ids if i]
    if not ids:
        return {}
    path = db_path or _resolve_db_path()
    pr_files: dict[str, list[str]] = {}
    labels: dict[str, list[str]] = {}
    try:
        with connect(path) as conn:
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"""
                SELECT item_id, ref_target_item_id, ref_source
                FROM item_external_refs
                WHERE ref_type = 'touches_component'
                  AND item_id IN ({placeholders})
                """,
                tuple(ids),
            )
            for row in cur.fetchall():
                target = str(row["ref_target_item_id"] or "")
                if not target.startswith("component:"):
                    continue
                comp = target[len("component:") :]
                src = str(row["ref_source"] or "")
                item_id = str(row["item_id"])
                if src.startswith("pr-files:"):
                    pr_files.setdefault(item_id, []).append(comp)
                elif src == "issue-label":
                    labels.setdefault(item_id, []).append(comp)
    except sqlite3.Error as exc:
        log.warning("db_readers.fetch_issue_components_from_db: %r", exc)
        return {}

    out: dict[str, list[str]] = {}
    for item_id in ids:
        if item_id in pr_files:
            # Dedup and sort for stable display order.
            out[item_id] = sorted(set(pr_files[item_id]))
        elif item_id in labels:
            out[item_id] = sorted(set(labels[item_id]))
        else:
            out[item_id] = []
    return out


# ---------------------------------------------------------------------------
# Review-queue readers (design + PR review).


def fetch_design_review_from_db(
    repos: Iterable[str],
    *,
    db_path: Optional[pathlib.Path] = None,
    now: Optional[datetime] = None,
) -> tuple[list[object], list[str]]:
    """Return ``(items, failed_repos)`` matching the GH fetcher shape.

    Returns a list of :class:`alice_forge.review_queue_renderer.DesignItem`
    instances — the same dataclass the pre-flip code produced, so the
    renderer sees no shape change.
    """
    # Imported inside the function to keep the module import graph
    # cheap for the common case where only ``fetch_issues_from_db``
    # is called.
    from alice_forge.review_queue_renderer import DesignItem, age_str

    path = db_path or _resolve_db_path()
    repos_set = set(repos)
    items: list[object] = []
    now_dt = now

    try:
        with connect(path) as conn:
            cur = conn.execute(
                """
                SELECT
                    i.item_id,
                    i.title,
                    i.created_at,
                    i.updated_at
                FROM items i
                JOIN state_machine sm ON sm.item_id = i.item_id
                WHERE i.item_type = 'gh:issue'
                  AND sm.state IN ('design_review', 'sm:design_review')
                ORDER BY i.updated_at DESC
                """
            )
            for row in cur.fetchall():
                parsed = _item_id_to_repo_number(row["item_id"])
                if parsed is None:
                    continue
                repo, number = parsed
                if repos_set and repo not in repos_set:
                    continue
                created_at = str(row["created_at"] or row["updated_at"] or "")
                items.append(
                    DesignItem(
                        repo=repo,
                        number=number,
                        title=str(row["title"] or "").strip(),
                        url=f"https://github.com/{repo}/issues/{number}",
                        age=age_str(created_at, now=now_dt) if created_at else "",
                    )
                )
    except sqlite3.Error as exc:
        log.warning("db_readers.fetch_design_review_from_db: %r", exc)
        return [], []

    return items, []


def fetch_pr_review_from_db(
    repos: Iterable[str],
    *,
    db_path: Optional[pathlib.Path] = None,
    now: Optional[datetime] = None,
) -> tuple[list[object], list[str]]:
    """Return the ``PRItem`` list for non-draft open PRs needing review.

    Shape matches
    :func:`alice_forge.review_queue_renderer.fetch_pr_review`.

    Caveat: the poller stores PR state as one of ``pr:open`` /
    ``pr:draft`` / ``pr:merged`` / ``pr:closed`` — it does NOT track
    ``reviewDecision`` (REVIEW_REQUIRED / APPROVED / …) because that's
    a derived field over PR reviews API that would double the poll
    cost. We therefore surface EVERY non-draft open PR here; the
    status site treats the output as "non-draft open PRs" (a strict
    superset of the pre-#790 "needs review" set). Rolling back to
    ``STATUS_SITE_READ_FROM=gh`` restores the stricter filter. A
    follow-up PR will add a reviewDecision column if the superset
    proves noisy — until then, prefer "more visible" over "less visible".
    """
    from alice_forge.review_queue_renderer import PRItem, age_str

    path = db_path or _resolve_db_path()
    repos_set = set(repos)
    items: list[object] = []
    now_dt = now

    try:
        with connect(path) as conn:
            cur = conn.execute(
                """
                SELECT
                    i.item_id,
                    i.title,
                    i.created_at,
                    i.updated_at,
                    sm.state,
                    i.author
                FROM items i
                JOIN state_machine sm ON sm.item_id = i.item_id
                WHERE i.item_type = 'gh:pr'
                  AND sm.state = 'pr:open'
                ORDER BY i.updated_at DESC
                """
            )
            for row in cur.fetchall():
                parsed = _pr_item_id_to_repo_number(row["item_id"])
                if parsed is None:
                    continue
                repo, number = parsed
                if repos_set and repo not in repos_set:
                    continue
                created_at = str(row["created_at"] or row["updated_at"] or "")
                items.append(
                    PRItem(
                        repo=repo,
                        number=number,
                        title=str(row["title"] or "").strip(),
                        url=f"https://github.com/{repo}/pull/{number}",
                        age=age_str(created_at, now=now_dt) if created_at else "",
                        author=str(row["author"] or ""),
                        ci="",
                    )
                )
    except sqlite3.Error as exc:
        log.warning("db_readers.fetch_pr_review_from_db: %r", exc)
        return [], []

    return items, []


# ---------------------------------------------------------------------------
# Done-today reader.


def fetch_merged_prs_from_db(
    repos: Iterable[str],
    *,
    today: Optional[date] = None,
    db_path: Optional[pathlib.Path] = None,
) -> tuple[list[object], list[str]]:
    """Return PRs merged on ``today`` (local date) across ``repos``.

    Shape matches
    :func:`status_site.sources.done_today.fetch_merged_prs`: a list of
    :class:`status_site.sources.done_today.MergedPR` dataclasses.

    The DB path reads merged-at timestamps from the ``amendments``
    rows written by the poller when a PR flips to the closed/merged
    state (``source='pr-merged-at'``). We filter by the same
    UTC-vs-local-date boundary logic the GH path uses.
    """
    from status_site.sources.done_today import MergedPR, today_local

    if today is None:
        today = today_local()
    path = db_path or _resolve_db_path()
    repos_set = set(repos)
    items: list[object] = []

    try:
        with connect(path) as conn:
            cur = conn.execute(
                """
                SELECT
                    i.item_id,
                    i.title,
                    a.body AS merged_at_body,
                    MAX(a.ts) AS merged_at_ts
                FROM items i
                JOIN amendments a ON a.item_id = i.item_id
                WHERE i.item_type = 'gh:pr'
                  AND a.source = 'pr-merged-at'
                GROUP BY i.item_id
                ORDER BY merged_at_ts DESC
                """
            )
            for row in cur.fetchall():
                parsed = _pr_item_id_to_repo_number(row["item_id"])
                if parsed is None:
                    continue
                repo, number = parsed
                if repos_set and repo not in repos_set:
                    continue
                merged_at = str(row["merged_at_body"] or row["merged_at_ts"] or "").strip()
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
                        number=number,
                        title=str(row["title"] or "").strip(),
                        url=f"https://github.com/{repo}/pull/{number}",
                        merged_at=merged_at,
                    )
                )
    except sqlite3.Error as exc:
        log.warning("db_readers.fetch_merged_prs_from_db: %r", exc)
        return [], []

    return items, []
