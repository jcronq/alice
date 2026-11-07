"""Memory-worker step: ``derive_tasks_workers_events``.

Walks three on-disk stores in ``~/alice-mind/`` and upserts the
rows into ``alice-state.db``:

1. ``inner/tasks/task-*/task.yaml`` → ``tasks``.
2. ``inner/notes/worker-*.json`` and ``inner/notes/worker-*-done.json``
   pairs → ``workers``.
3. ``memory/events.jsonl`` → ``events`` (byte-offset-incremental tail).

Each pass is idempotent. The incremental gate is the last
successful ``derive_runs.started_at`` timestamp + the last
``derive_runs.last_event_offset`` byte offset. Running the derive
twice in a row with no filesystem changes produces zero upserts.

The module has NO dependency on Alice's runtime (speaking, thinking,
the kernel); it's a pure walker so the memory worker can call it and
``tests/test_alice_state_db.py`` can exercise it against a tmp-path
vault.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .schema import apply_schema

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass — returned to the memory worker so its heartbeat
# event can carry the derive outcome.


@dataclasses.dataclass
class DeriveReport:
    """Summary of one derive pass. Used by the memory-worker heartbeat.

    ``errors`` is a list of ``(kind, path, reason)`` triples — kept
    structured rather than stringified so the next derive run can
    log a ``derive_runs.errors_json`` payload downstream readers can
    filter on.
    """

    tasks_upserted: int = 0
    workers_upserted: int = 0
    events_appended: int = 0
    last_event_offset: int = 0
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks_upserted": self.tasks_upserted,
            "workers_upserted": self.workers_upserted,
            "events_appended": self.events_appended,
            "last_event_offset": self.last_event_offset,
            "error_count": len(self.errors),
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# Repo/issue parsing helpers


_GH_REPO_RE = re.compile(r"\b([\w.\-]+/[\w.\-]+)#(\d+)\b")
_ISSUE_TAG_RE = re.compile(
    r"github-issue:([\w.\-]+/[\w.\-]+)#(\d+)", re.IGNORECASE
)
_SHORT_ISSUE_RE = re.compile(r"\b#(\d+)\b")
_REPO_TAG_RE = re.compile(r"^([\w.\-]+/[\w.\-]+)$")


def _parse_repo_issue(
    source: Optional[str], tags: list[str]
) -> tuple[Optional[str], Optional[int]]:
    """Pull ``(repo, issue_number)`` out of a task's ``source`` + tags.

    ``source`` is the primary signal — the auto-fix flow writes
    ``jcronq/alice#784`` there verbatim. Tags are the fallback for
    older tasks that only carry ``github-issue:jcronq/alice#630``
    + a bare ``jcronq/alice`` tag. Returning ``(None, None)`` is
    normal for non-GH tasks.
    """
    if source:
        m = _GH_REPO_RE.search(source)
        if m:
            return m.group(1), int(m.group(2))

    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        m = _ISSUE_TAG_RE.search(tag)
        if m:
            return m.group(1), int(m.group(2))

    # Last-ditch: a tag that's exactly ``owner/repo`` paired with a
    # bare ``#<N>`` in source. Rare — covers the hand-written edge
    # cases in task-0001 etc.
    repo = None
    for tag in tags or []:
        if isinstance(tag, str) and _REPO_TAG_RE.match(tag):
            repo = tag
            break
    if repo and source:
        m = _SHORT_ISSUE_RE.search(source)
        if m:
            return repo, int(m.group(1))

    return None, None


# ---------------------------------------------------------------------------
# YAML load — mirrors alice_forge.task_store._parse_yaml_lenient so
# derive can load a task.yaml without taking a hard dep on the CLI
# module (which drags in argparse + its import-time side effects).


def _load_task_yaml(path: pathlib.Path) -> Optional[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml as _yaml

        data = _yaml.safe_load(text)
    except ImportError:  # pragma: no cover — yaml is a hard dep
        return None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Tasks derive


def _derive_tasks(
    conn: sqlite3.Connection,
    tasks_root: pathlib.Path,
    report: DeriveReport,
    since_mtime_ns: Optional[int],
) -> None:
    """Upsert one row per ``task-*/task.yaml`` whose mtime_ns changed.

    ``since_mtime_ns`` is the maximum mtime observed on the previous
    pass; files with ``mtime_ns <= since_mtime_ns`` are skipped. This
    keeps the walk O(changes) on steady state.
    """
    if not tasks_root.is_dir():
        return

    for child in sorted(tasks_root.iterdir()):
        if not child.is_dir():
            continue
        if not child.name.startswith("task-"):
            continue
        yaml_path = child / "task.yaml"
        if not yaml_path.is_file():
            # Orphan directory (no task.yaml) — skip silently; issue
            # #781 covers the index.jsonl staleness side of this.
            continue
        try:
            mtime_ns = yaml_path.stat().st_mtime_ns
        except OSError as exc:
            report.errors.append(
                {"kind": "task", "path": str(yaml_path), "reason": str(exc)}
            )
            continue
        if since_mtime_ns is not None and mtime_ns <= since_mtime_ns:
            continue

        data = _load_task_yaml(yaml_path)
        if data is None:
            report.errors.append(
                {
                    "kind": "task",
                    "path": str(yaml_path),
                    "reason": "task.yaml did not parse to a mapping",
                }
            )
            continue

        tags = data.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tags = [str(t) for t in tags]

        source = data.get("source")
        source_str = str(source) if source is not None else None
        repo, issue_number = _parse_repo_issue(source_str, tags)

        task_id = str(data.get("id") or child.name)
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, status, actor, artifact_type, source,
                gh_repo, gh_issue_number, created, updated,
                tags_json, task_yaml_path, task_dir_path, mtime_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                actor = excluded.actor,
                artifact_type = excluded.artifact_type,
                source = excluded.source,
                gh_repo = excluded.gh_repo,
                gh_issue_number = excluded.gh_issue_number,
                created = excluded.created,
                updated = excluded.updated,
                tags_json = excluded.tags_json,
                task_yaml_path = excluded.task_yaml_path,
                task_dir_path = excluded.task_dir_path,
                mtime_ns = excluded.mtime_ns
            """,
            (
                task_id,
                str(data.get("title") or ""),
                str(data.get("status") or "draft"),
                str(data.get("actor") or ""),
                str(data.get("artifact_type") or ""),
                source_str,
                repo,
                issue_number,
                str(data.get("created") or ""),
                str(data.get("updated") or ""),
                json.dumps(tags),
                str(yaml_path),
                str(child),
                mtime_ns,
            ),
        )
        report.tasks_upserted += 1


# ---------------------------------------------------------------------------
# Workers derive


# bg_ids are usually ``bg-<uuid4-hex>`` but tests + ad-hoc tooling
# occasionally use ``bg-worker0``-style labels, so accept any non-dot
# chars after the ``bg-`` prefix. The ``-done.json`` match has to
# come first so a done marker isn't parsed as a dispatch marker with
# bg_id ``bg-xxx-done``.
_WORKER_DISPATCH_RE = re.compile(r"^worker-(bg-[A-Za-z0-9_\-]+)\.json$")
_WORKER_DONE_RE = re.compile(r"^worker-(bg-[A-Za-z0-9_\-]+)-done\.json$")


def _load_worker_json(path: pathlib.Path) -> Optional[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _derive_workers(
    conn: sqlite3.Connection,
    notes_root: pathlib.Path,
    report: DeriveReport,
) -> None:
    """Merge ``worker-<bg_id>.json`` + ``worker-<bg_id>-done.json``.

    The derive walks ``inner/notes/`` + ``inner/notes/.consumed/``
    (dated subfolders) because Stage B atomic-renames the two marker
    files into ``.consumed/<date>/`` once they've been promoted; the
    workers table has to find them in either location until the
    rolling-7-day delete sweeps them.

    Pairing is by ``bg_id``. A lone dispatch marker produces a row
    with ``outcome=NULL``; a lone done marker is treated as
    half-pair and still upserted (defensive — if the dispatch marker
    went missing, the done file carries most of the useful fields).
    """
    if not notes_root.is_dir():
        return

    dispatch: dict[str, pathlib.Path] = {}
    done: dict[str, pathlib.Path] = {}

    def _scan(root: pathlib.Path) -> None:
        for child in root.iterdir():
            if not child.is_file():
                continue
            name = child.name
            m = _WORKER_DONE_RE.match(name)
            if m:
                done[m.group(1)] = child
                continue
            m = _WORKER_DISPATCH_RE.match(name)
            if m:
                dispatch[m.group(1)] = child

    _scan(notes_root)
    consumed_root = notes_root / ".consumed"
    if consumed_root.is_dir():
        for day_dir in consumed_root.iterdir():
            if day_dir.is_dir():
                _scan(day_dir)

    all_bg_ids = set(dispatch.keys()) | set(done.keys())
    for bg_id in sorted(all_bg_ids):
        disp_path = dispatch.get(bg_id)
        done_path = done.get(bg_id)
        source_path = disp_path or done_path
        assert source_path is not None  # one of the two is present by construction

        try:
            mtime_ns = source_path.stat().st_mtime_ns
        except OSError as exc:
            report.errors.append(
                {"kind": "worker", "path": str(source_path), "reason": str(exc)}
            )
            continue
        if done_path is not None:
            try:
                mtime_ns = max(mtime_ns, done_path.stat().st_mtime_ns)
            except OSError:
                pass

        disp = _load_worker_json(disp_path) if disp_path else None
        done_data = _load_worker_json(done_path) if done_path else None

        if disp is None and done_data is None:
            report.errors.append(
                {
                    "kind": "worker",
                    "path": str(source_path),
                    "reason": "neither marker parsed as JSON",
                }
            )
            continue

        merged: dict[str, Any] = {}
        if disp:
            merged.update(disp)
        if done_data:
            # done fields override dispatch fields where they overlap
            # (outcome / pr_url / finished_at only come from done).
            merged.update(done_data)

        conn.execute(
            """
            INSERT INTO workers (
                bg_id, kind, label, description, repo, gh_issue,
                task_id, surface_path, pr_url, commit_sha,
                started_at, finished_at, outcome,
                dispatch_path, done_path, stdout_path, worktree_path,
                mtime_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bg_id) DO UPDATE SET
                kind = excluded.kind,
                label = excluded.label,
                description = excluded.description,
                repo = excluded.repo,
                gh_issue = excluded.gh_issue,
                task_id = excluded.task_id,
                surface_path = excluded.surface_path,
                pr_url = excluded.pr_url,
                commit_sha = excluded.commit_sha,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                outcome = excluded.outcome,
                dispatch_path = excluded.dispatch_path,
                done_path = excluded.done_path,
                stdout_path = excluded.stdout_path,
                worktree_path = excluded.worktree_path,
                mtime_ns = excluded.mtime_ns
            """,
            (
                bg_id,
                _opt_str(merged.get("kind")),
                _opt_str(merged.get("label")),
                _opt_str(merged.get("description")),
                _opt_str(merged.get("repo") or merged.get("gh_repo")),
                _opt_int(merged.get("gh_issue") or merged.get("gh_issue_number")),
                _opt_str(merged.get("task_id")),
                _opt_str(merged.get("surface_path")),
                _opt_str(merged.get("pr_url")),
                _opt_str(merged.get("commit_sha")),
                _opt_str(merged.get("started_at")),
                _opt_str(merged.get("finished_at")),
                _opt_str(merged.get("outcome")),
                str(disp_path) if disp_path else str(done_path),
                str(done_path) if done_path else None,
                _opt_str(merged.get("stdout_path")),
                _opt_str(merged.get("worktree_path")),
                mtime_ns,
            ),
        )
        report.workers_upserted += 1


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Events tail


def _derive_events(
    conn: sqlite3.Connection,
    events_path: pathlib.Path,
    report: DeriveReport,
    last_offset: int,
) -> None:
    """Tail ``events.jsonl`` from ``last_offset``, append new rows.

    Reads bytes-at-a-time rather than ``readlines()`` so the next
    pass can resume cleanly from ``report.last_event_offset``. A
    truncated final line is left for the next pass to pick up once
    the writer finishes it.
    """
    if not events_path.is_file():
        report.last_event_offset = last_offset
        return

    try:
        file_size = events_path.stat().st_size
    except OSError as exc:
        report.errors.append(
            {"kind": "events", "path": str(events_path), "reason": str(exc)}
        )
        report.last_event_offset = last_offset
        return

    if file_size < last_offset:
        # File was truncated/rewritten under us. Reset to 0 and
        # re-derive from scratch; the UPSERT path isn't available for
        # events (byte offset is the PK), but duplicates are harmless
        # because ``line_id`` is just the running offset — downstream
        # readers should not depend on stable ``line_id`` across
        # events.jsonl rewrites. Log it.
        log.warning(
            "events.jsonl size (%d) < last_offset (%d); restarting tail",
            file_size,
            last_offset,
        )
        conn.execute("DELETE FROM events;")
        last_offset = 0

    cursor_offset = last_offset
    with events_path.open("rb") as fh:
        fh.seek(last_offset)
        while True:
            line_start = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                # Partial trailing write — stop here, resume next pass.
                break
            line_end = fh.tell()
            stripped = raw.strip()
            if not stripped:
                cursor_offset = line_end
                continue
            try:
                data = json.loads(stripped.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                report.errors.append(
                    {
                        "kind": "events",
                        "path": f"{events_path}:{line_start}",
                        "reason": "line did not parse as JSON",
                    }
                )
                cursor_offset = line_end
                continue
            if not isinstance(data, dict):
                cursor_offset = line_end
                continue

            repo, issue_number = _event_repo_issue(data)
            pr_number = _event_pr_number(data)
            task_id = _event_task_id(data)

            conn.execute(
                """
                INSERT INTO events (
                    line_id, byte_offset, ts, type, subject, source,
                    repo, gh_issue_number, pr_number, task_id, body_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(line_id) DO UPDATE SET
                    byte_offset = excluded.byte_offset,
                    ts = excluded.ts,
                    type = excluded.type,
                    subject = excluded.subject,
                    source = excluded.source,
                    repo = excluded.repo,
                    gh_issue_number = excluded.gh_issue_number,
                    pr_number = excluded.pr_number,
                    task_id = excluded.task_id,
                    body_json = excluded.body_json
                """,
                (
                    line_start,
                    line_start,
                    _opt_str(data.get("ts")),
                    _opt_str(data.get("type")),
                    _opt_str(data.get("subject")),
                    _opt_str(data.get("source")),
                    repo,
                    issue_number,
                    pr_number,
                    task_id,
                    stripped.decode("utf-8", errors="replace"),
                ),
            )
            report.events_appended += 1
            cursor_offset = line_end

    report.last_event_offset = cursor_offset


def _event_repo_issue(
    data: dict[str, Any],
) -> tuple[Optional[str], Optional[int]]:
    """Pull repo + issue number from an events.jsonl line.

    Known patterns in the stream:
    * ``type: github_issue_state`` with ``repo``/``number``
    * ``type: github_pr_state`` with ``repo``/``pr_number`` (handled
      by :func:`_event_pr_number`; this fn returns only issue linkage)
    * nested ``data.repo`` + ``data.gh_issue_number``
    * ``source: jcronq/alice#784`` style (used by the auto-fix flow)
    """
    repo = data.get("repo") or data.get("gh_repo")
    issue = data.get("gh_issue_number") or data.get("issue_number")
    if not (repo and issue):
        nested = data.get("data")
        if isinstance(nested, dict):
            repo = repo or nested.get("repo") or nested.get("gh_repo")
            issue = (
                issue
                or nested.get("gh_issue_number")
                or nested.get("issue_number")
                or nested.get("number")
            )
    if not (repo and issue):
        source = data.get("source")
        if isinstance(source, str):
            m = _GH_REPO_RE.search(source)
            if m:
                repo = repo or m.group(1)
                issue = issue or int(m.group(2))
    return (_opt_str(repo), _opt_int(issue))


def _event_pr_number(data: dict[str, Any]) -> Optional[int]:
    pr = data.get("pr_number")
    if pr is None:
        nested = data.get("data")
        if isinstance(nested, dict):
            pr = nested.get("pr_number") or nested.get("number")
            if data.get("type") and "pr" not in str(data.get("type")):
                # Guard: a nested ``number`` on an issue event isn't a
                # PR number. Only trust it when the event type hints PR.
                pr = nested.get("pr_number")
    return _opt_int(pr)


def _event_task_id(data: dict[str, Any]) -> Optional[str]:
    task_id = data.get("task_id")
    if task_id is None:
        nested = data.get("data")
        if isinstance(nested, dict):
            task_id = nested.get("task_id")
    if isinstance(task_id, str) and task_id.startswith("task-"):
        return task_id
    return None


# ---------------------------------------------------------------------------
# Public entry point


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_tasks_workers_events(
    mind: pathlib.Path,
    db_path: Optional[pathlib.Path] = None,
) -> DeriveReport:
    """Run the PR 1 derive pass over ``mind``.

    Args:
        mind: Root of the alice-mind tree (contains ``inner/`` +
            ``memory/``).
        db_path: Override for ``alice-state.db`` location. Defaults
            to ``<mind>/inner/state/alice-state.db``.

    Returns:
        A :class:`DeriveReport` with upsert counts + any errors.
        The report is also persisted to ``derive_runs`` so later
        incremental passes can gate on ``started_at`` + the stored
        ``last_event_offset``.
    """
    started_at = _utc_now_iso()
    started_mono = time.monotonic()

    mind = pathlib.Path(mind)
    tasks_root = mind / "inner" / "tasks"
    notes_root = mind / "inner" / "notes"
    events_path = mind / "memory" / "events.jsonl"

    if db_path is None:
        db_path = mind / "inner" / "state" / "alice-state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    report = DeriveReport()
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)

        # Pull the last-derive offset + mtime watermark. A fresh DB
        # returns (NULL, 0) → first pass is a full walk.
        row = conn.execute(
            """
            SELECT MAX(mtime_ns), MAX(last_event_offset)
            FROM (
                SELECT NULL AS mtime_ns, last_event_offset
                FROM derive_runs
                WHERE step = 'derive_tasks_workers_events'
                ORDER BY rowid DESC LIMIT 1
            )
            """
        ).fetchone()
        last_event_offset = int(row[1] or 0) if row else 0

        # Task watermark lives on the tasks table itself — safer than
        # trusting a run timestamp (task.yaml mtime can jitter).
        row = conn.execute(
            "SELECT COALESCE(MAX(mtime_ns), 0) FROM tasks"
        ).fetchone()
        tasks_watermark = int(row[0] or 0) if row else 0

        _derive_tasks(
            conn,
            tasks_root,
            report,
            since_mtime_ns=(tasks_watermark or None),
        )
        _derive_workers(conn, notes_root, report)
        _derive_events(conn, events_path, report, last_event_offset)

        finished_at = _utc_now_iso()
        report.duration_ms = int((time.monotonic() - started_mono) * 1000)
        conn.execute(
            """
            INSERT INTO derive_runs (
                step, started_at, finished_at, rows_touched,
                tasks_upserted, workers_upserted, events_appended,
                last_event_offset, errors_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "derive_tasks_workers_events",
                started_at,
                finished_at,
                report.tasks_upserted
                + report.workers_upserted
                + report.events_appended,
                report.tasks_upserted,
                report.workers_upserted,
                report.events_appended,
                report.last_event_offset,
                json.dumps(report.errors),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return report
