"""Read helpers over ``alice-state.db``.

Read-only API consumed by:

* The ``task db ...`` CLI subcommand (see
  :func:`alice_forge.task_cli._cmd_db_*`).
* Any other reader that wants to cross-join tasks/workers/events
  without running its own SQL (status-site, future dispatcher
  idempotency checks, etc.).

Rows come back as plain dicts so JSON serialisation is cheap. The
helpers normalise ``tags_json`` to a list and ``body_json`` to a
parsed dict so callers don't need to know the wire shape.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import pathlib
import sqlite3
from typing import Any, Iterator, Optional

from .schema import apply_schema

#: Canonical location. The memory worker + CLI default here; tests
#: pass their own ``db_path`` into :func:`connect`.
DEFAULT_DB_PATH = (
    pathlib.Path.home() / "alice-mind" / "inner" / "state" / "alice-state.db"
)


@dataclasses.dataclass(frozen=True)
class WorkerRow:
    """Lightweight shape for a single ``workers`` row.

    The CLI formats these; tests introspect them directly. Kept as
    a frozen dataclass so it's trivially JSON-serialisable and
    orderable by ``started_at``.
    """

    bg_id: str
    kind: Optional[str]
    repo: Optional[str]
    gh_issue: Optional[int]
    task_id: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]
    outcome: Optional[str]
    pr_url: Optional[str]
    description: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkerRow":
        return cls(
            bg_id=row["bg_id"],
            kind=row["kind"],
            repo=row["repo"],
            gh_issue=row["gh_issue"],
            task_id=row["task_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            outcome=row["outcome"],
            pr_url=row["pr_url"],
            description=row["description"],
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@contextlib.contextmanager
def connect(
    db_path: Optional[pathlib.Path] = None, *, read_only: bool = True
) -> Iterator[sqlite3.Connection]:
    """Yield a ``sqlite3.Connection`` pointed at the derived DB.

    Opens via ``file:...?mode=ro`` for readers (WAL-safe). When
    ``read_only=False`` (the derive pass) returns a RW connection —
    callers are expected to apply the schema themselves, same as
    :func:`derive.derive_tasks_workers_events`.

    The returned connection has ``row_factory=sqlite3.Row`` so the
    helpers below can use dict-style access.
    """
    path = pathlib.Path(db_path or DEFAULT_DB_PATH)
    if read_only and not path.exists():
        # Create an empty DB so RO callers get the shape they expect
        # even on first-ever-run. Cheaper than raising + catching.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = sqlite3.connect(str(path))
        try:
            apply_schema(tmp)
        finally:
            tmp.close()

    if read_only:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys()}
    if "tags_json" in out and out["tags_json"]:
        try:
            out["tags"] = json.loads(out["tags_json"])
        except json.JSONDecodeError:
            out["tags"] = []
    if "body_json" in out and out["body_json"]:
        try:
            out["body"] = json.loads(out["body_json"])
        except json.JSONDecodeError:
            out["body"] = None
    return out


def _parse_gh_qualifier(qualifier: str) -> tuple[str, int]:
    """Parse a ``jcronq/alice#784``-style qualifier.

    Returns ``(repo, issue_number)``. Raises ``ValueError`` on
    malformed input. The repo half is returned verbatim (slash
    included); callers that need the org/name split can partition
    further.
    """
    if "#" not in qualifier:
        raise ValueError(
            f"expected 'org/repo#<N>' qualifier, got {qualifier!r}"
        )
    repo, _, num = qualifier.partition("#")
    try:
        return repo, int(num)
    except ValueError as exc:
        raise ValueError(
            f"issue number not an integer in {qualifier!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Task helpers


def tasks_for_issue(
    conn: sqlite3.Connection, repo: str, number: int
) -> list[dict[str, Any]]:
    """Return every task whose source/tags mention ``<repo>#<number>``."""
    cur = conn.execute(
        """
        SELECT * FROM tasks
        WHERE gh_repo = ? AND gh_issue_number = ?
        ORDER BY updated DESC
        """,
        (repo, number),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def task_by_id(
    conn: sqlite3.Connection, task_id: str
) -> Optional[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Worker helpers


def workers_for_task(
    conn: sqlite3.Connection, task_id: str
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT * FROM workers
        WHERE task_id = ?
        ORDER BY started_at DESC
        """,
        (task_id,),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def workers_for_issue(
    conn: sqlite3.Connection, repo: str, number: int
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT * FROM workers
        WHERE repo = ? AND gh_issue = ?
        ORDER BY started_at DESC
        """,
        (repo, number),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def workers_for_bg_id(
    conn: sqlite3.Connection, bg_id: str
) -> Optional[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM workers WHERE bg_id = ?", (bg_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_workers(
    conn: sqlite3.Connection,
    *,
    outcome: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    sql = "SELECT * FROM workers"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return [_row_to_dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Event helpers


def events_for_issue(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT * FROM events
        WHERE repo = ? AND gh_issue_number = ?
        ORDER BY ts DESC LIMIT ?
        """,
        (repo, number, int(limit)),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def events_for_task(
    conn: sqlite3.Connection, task_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT * FROM events
        WHERE task_id = ?
        ORDER BY ts DESC LIMIT ?
        """,
        (task_id, int(limit)),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def list_events(
    conn: sqlite3.Connection,
    *,
    type: Optional[str] = None,  # noqa: A002 — mirrors CLI flag name
    since: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if type is not None:
        clauses.append("type = ?")
        params.append(type)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    sql = "SELECT * FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return [_row_to_dict(r) for r in cur.fetchall()]


def list_tasks(
    conn: sqlite3.Connection,
    *,
    gh_issue: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by ``<owner>/<repo>#<N>`` or status."""
    clauses: list[str] = []
    params: list[Any] = []
    if gh_issue is not None:
        repo, number = _parse_gh_qualifier(gh_issue)
        clauses.append("gh_repo = ? AND gh_issue_number = ?")
        params.extend([repo, number])
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM tasks"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    return [_row_to_dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cross-store snapshot — the issue-#789 success check


def state_of(
    conn: sqlite3.Connection, qualifier: str, *, events_limit: int = 20
) -> dict[str, Any]:
    """Return task + workers + events for a ``<repo>#<N>`` qualifier.

    This is the specific join issue #789 names as the PR 1 success
    check: ``task db state jcronq/alice#784`` should return the task
    row, every worker touching it, and recent related events in one
    call. Later PRs will fold in surfaces, PR state, and GH issue
    metadata.
    """
    repo, number = _parse_gh_qualifier(qualifier)
    tasks = tasks_for_issue(conn, repo, number)
    workers = workers_for_issue(conn, repo, number)
    # Also pull workers attached to any linked task_id (covers the
    # case where the worker file only carries task_id, not repo).
    seen = {w["bg_id"] for w in workers}
    for task in tasks:
        for w in workers_for_task(conn, task["id"]):
            if w["bg_id"] not in seen:
                workers.append(w)
                seen.add(w["bg_id"])
    events = events_for_issue(conn, repo, number, limit=events_limit)
    # Walk task-linked events too so a GH-less transition still shows.
    seen_events = {e["line_id"] for e in events}
    for task in tasks:
        for e in events_for_task(conn, task["id"], limit=events_limit):
            if e["line_id"] not in seen_events:
                events.append(e)
                seen_events.add(e["line_id"])
    events.sort(key=lambda e: (e.get("ts") or ""), reverse=True)
    events = events[:events_limit]

    return {
        "qualifier": qualifier,
        "repo": repo,
        "issue_number": number,
        "tasks": tasks,
        "workers": workers,
        "events": events,
    }
