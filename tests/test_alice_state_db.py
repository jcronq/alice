"""Tests for the alice-state.db derived index (issue #789 PR 1).

Each test seeds a tmp-path alice-mind tree with a handful of
tasks / worker markers / events.jsonl lines, runs
:func:`derive.derive_tasks_workers_events` against it, and asserts
on the SQLite state afterwards.

Coverage:

* Fresh-DB derive materialises the right rows across all three
  stores.
* Incremental derive is a no-op when nothing changed (mtime gate
  + byte offset gate).
* Updated task.yaml mtime → upsert reflects the new status.
* ``worker-<bg_id>.json`` alone vs. paired with a done marker →
  row carries ``outcome`` iff done marker present.
* ``state_of()`` joins task + workers + events for a GH qualifier.
* ``task db state`` CLI returns the same payload.
* Error rows are captured in ``derive_runs.errors_json`` without
  raising.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import time

import pytest

from alice_forge import task_cli
from alice_state_db import derive, read_api
from alice_state_db import schema as schema_mod
from alice_speaking import worker_markers


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tmp alice-mind tree with the directory shape derive expects."""
    (tmp_path / "inner" / "tasks").mkdir(parents=True)
    (tmp_path / "inner" / "notes").mkdir(parents=True)
    (tmp_path / "inner" / "state").mkdir(parents=True)
    (tmp_path / "memory").mkdir(parents=True)
    return tmp_path


def _write_task_yaml(
    mind: pathlib.Path,
    task_id: str,
    *,
    title: str = "x",
    status: str = "draft",
    source: str = "speaking",
    tags: list[str] | None = None,
    merge_ref: str | None = None,
) -> pathlib.Path:
    task_dir = mind / "inner" / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = task_dir / "task.yaml"
    body = [
        f"id: {task_id}",
        f"title: {json.dumps(title)}",
        "created: '2026-11-06T00:00:00+00:00'",
        "updated: '2026-11-06T00:00:00+00:00'",
        f"status: {status}",
        "actor: alice",
        "artifact_type: code",
        f"source: {json.dumps(source)}",
    ]
    if tags:
        body.append("tags:")
        for t in tags:
            body.append(f"- {json.dumps(t)}")
    else:
        body.append("tags: []")
    if merge_ref:
        body.append(f"merge_ref: {merge_ref}")
    yaml_path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return yaml_path


def _append_event(mind: pathlib.Path, line: dict) -> None:
    path = mind / "memory" / "events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _db_path(mind: pathlib.Path) -> pathlib.Path:
    return mind / "inner" / "state" / "alice-state.db"


# ---------------------------------------------------------------------------
# Schema


def test_apply_schema_is_idempotent(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db))
    schema_mod.apply_schema(conn)
    # Run twice — must not raise.
    schema_mod.apply_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert tables.issuperset(
        {"tasks", "workers", "events", "derive_runs"}
    )
    # WAL journal mode is set.
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


# ---------------------------------------------------------------------------
# Tasks derive


def test_derive_tasks_from_yaml(mind: pathlib.Path) -> None:
    _write_task_yaml(
        mind,
        "task-0784",
        title="feat: unified state DB",
        status="building",
        source="jcronq/alice#789",
        tags=["github-issue:jcronq/alice#789", "jcronq/alice"],
    )
    _write_task_yaml(
        mind,
        "task-0785",
        title="unrelated",
        status="done",
        source="speaking",
        tags=["chore"],
    )

    report = derive.derive_tasks_workers_events(mind)
    assert report.tasks_upserted == 2

    with read_api.connect(_db_path(mind)) as conn:
        rows = read_api.list_tasks(conn, limit=10)
        by_id = {r["id"]: r for r in rows}
        assert by_id["task-0784"]["gh_repo"] == "jcronq/alice"
        assert by_id["task-0784"]["gh_issue_number"] == 789
        assert by_id["task-0784"]["status"] == "building"
        # Tags round-trip as a list.
        assert "jcronq/alice" in by_id["task-0784"]["tags"]
        assert by_id["task-0785"]["gh_repo"] is None


def test_derive_tasks_incremental_is_noop_when_unchanged(
    mind: pathlib.Path,
) -> None:
    _write_task_yaml(mind, "task-0100", title="first")
    derive.derive_tasks_workers_events(mind)

    report = derive.derive_tasks_workers_events(mind)
    assert report.tasks_upserted == 0


def test_derive_tasks_picks_up_mtime_bump(mind: pathlib.Path) -> None:
    yaml_path = _write_task_yaml(mind, "task-0101", status="draft")
    derive.derive_tasks_workers_events(mind)

    # Bump mtime and content — the status flip must land in the DB.
    time.sleep(0.01)
    _write_task_yaml(mind, "task-0101", status="selected")
    # Force a visible mtime change in case the write happened too
    # fast for the filesystem's ns resolution to differ.
    new_ns = yaml_path.stat().st_mtime_ns + 1_000_000
    os.utime(yaml_path, ns=(new_ns, new_ns))

    report = derive.derive_tasks_workers_events(mind)
    assert report.tasks_upserted == 1
    with read_api.connect(_db_path(mind)) as conn:
        task = read_api.task_by_id(conn, "task-0101")
    assert task is not None
    assert task["status"] == "selected"


# ---------------------------------------------------------------------------
# Workers derive


def test_derive_workers_dispatch_only(mind: pathlib.Path) -> None:
    worker_markers.write_dispatch_marker(
        mind,
        bg_id="bg-abc123",
        description="auto-fix 789",
    )
    derive.derive_tasks_workers_events(mind)

    with read_api.connect(_db_path(mind)) as conn:
        row = read_api.workers_for_bg_id(conn, "bg-abc123")
    assert row is not None
    assert row["bg_id"] == "bg-abc123"
    assert row["outcome"] is None
    assert row["started_at"] is not None


def test_derive_workers_dispatch_plus_done(mind: pathlib.Path) -> None:
    worker_markers.write_dispatch_marker(
        mind,
        bg_id="bg-def456",
        description="auto-fix",
    )
    worker_markers.write_done_marker(
        mind,
        bg_id="bg-def456",
        outcome="pr_opened",
        pr_url="https://github.com/jcronq/alice/pull/9999",
        commit_sha="deadbeef",
    )
    derive.derive_tasks_workers_events(mind)

    with read_api.connect(_db_path(mind)) as conn:
        row = read_api.workers_for_bg_id(conn, "bg-def456")
    assert row is not None
    assert row["outcome"] == "pr_opened"
    assert row["pr_url"] == "https://github.com/jcronq/alice/pull/9999"
    assert row["commit_sha"] == "deadbeef"


def test_derive_workers_finds_consumed_markers(mind: pathlib.Path) -> None:
    """Worker markers atomic-renamed under .consumed/<date>/ still count."""
    consumed = mind / "inner" / "notes" / ".consumed" / "2026-11-06"
    consumed.mkdir(parents=True)
    (consumed / "worker-bg-aged00.json").write_text(
        json.dumps(
            {
                "bg_id": "bg-aged00",
                "description": "old auto-fix",
                "started_at": "2026-11-06T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (consumed / "worker-bg-aged00-done.json").write_text(
        json.dumps(
            {
                "bg_id": "bg-aged00",
                "outcome": "pr_opened",
                "pr_url": "https://example.com/pr/1",
                "finished_at": "2026-11-06T00:05:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    derive.derive_tasks_workers_events(mind)

    with read_api.connect(_db_path(mind)) as conn:
        row = read_api.workers_for_bg_id(conn, "bg-aged00")
    assert row is not None
    assert row["outcome"] == "pr_opened"


# ---------------------------------------------------------------------------
# Events tail


def test_derive_events_byte_offset_incremental(mind: pathlib.Path) -> None:
    _append_event(
        mind,
        {
            "ts": "2026-11-06T00:00:00+00:00",
            "type": "workout",
            "subject": "jason",
        },
    )
    _append_event(
        mind,
        {
            "ts": "2026-11-06T00:01:00+00:00",
            "type": "github_pr_merged",
            "repo": "jcronq/alice",
            "gh_issue_number": 789,
        },
    )

    report = derive.derive_tasks_workers_events(mind)
    assert report.events_appended == 2

    # Second pass with no new lines — zero new events.
    report = derive.derive_tasks_workers_events(mind)
    assert report.events_appended == 0

    # Append one more; third pass picks it up incrementally.
    _append_event(
        mind,
        {
            "ts": "2026-11-06T00:02:00+00:00",
            "type": "note",
            "subject": "alice",
        },
    )
    report = derive.derive_tasks_workers_events(mind)
    assert report.events_appended == 1


def test_derive_events_extracts_gh_linkage(mind: pathlib.Path) -> None:
    _append_event(
        mind,
        {
            "ts": "2026-11-06T00:01:00+00:00",
            "type": "github_pr_state",
            "repo": "jcronq/alice",
            "gh_issue_number": 789,
            "pr_number": 791,
        },
    )
    _append_event(
        mind,
        {
            "ts": "2026-11-06T00:02:00+00:00",
            "type": "sm_task_transition",
            "task_id": "task-0784",
        },
    )

    derive.derive_tasks_workers_events(mind)

    with read_api.connect(_db_path(mind)) as conn:
        pr_events = read_api.events_for_issue(conn, "jcronq/alice", 789)
        task_events = read_api.events_for_task(conn, "task-0784")
    assert len(pr_events) == 1
    assert pr_events[0]["pr_number"] == 791
    assert len(task_events) == 1


def test_derive_events_tolerates_bad_json(mind: pathlib.Path) -> None:
    # Append one good line + one malformed line so the pass must
    # skip the bad line without raising.
    _append_event(mind, {"ts": "2026-11-06T00:00:00+00:00", "type": "ok"})
    path = mind / "memory" / "events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("this is not valid json\n")
    _append_event(mind, {"ts": "2026-11-06T00:01:00+00:00", "type": "ok2"})

    report = derive.derive_tasks_workers_events(mind)
    assert report.events_appended == 2
    assert any(err["kind"] == "events" for err in report.errors)


# ---------------------------------------------------------------------------
# state_of — issue #789 PR 1 success check


def test_state_of_joins_task_workers_events(mind: pathlib.Path) -> None:
    _write_task_yaml(
        mind,
        "task-0784",
        title="worker test",
        status="building",
        source="jcronq/alice#789",
        tags=["github-issue:jcronq/alice#789"],
    )
    worker_markers.write_dispatch_marker(
        mind,
        bg_id="bg-aaa111",
        description="auto-fix",
        repo="jcronq/alice",
        gh_issue=789,
        task_id="task-0784",
    )
    worker_markers.write_done_marker(
        mind,
        bg_id="bg-aaa111",
        outcome="pr_opened",
        pr_url="https://github.com/jcronq/alice/pull/791",
    )
    _append_event(
        mind,
        {
            "ts": "2026-11-06T00:01:00+00:00",
            "type": "github_pr_merged",
            "repo": "jcronq/alice",
            "gh_issue_number": 789,
        },
    )

    derive.derive_tasks_workers_events(mind)

    with read_api.connect(_db_path(mind)) as conn:
        payload = read_api.state_of(conn, "jcronq/alice#789")

    assert payload["repo"] == "jcronq/alice"
    assert payload["issue_number"] == 789
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["id"] == "task-0784"
    assert any(w["bg_id"] == "bg-aaa111" for w in payload["workers"])
    assert any(
        e.get("type") == "github_pr_merged" for e in payload["events"]
    )


def test_state_of_malformed_qualifier_raises(mind: pathlib.Path) -> None:
    with read_api.connect(_db_path(mind)) as conn:
        with pytest.raises(ValueError):
            read_api.state_of(conn, "not-a-qualifier")


# ---------------------------------------------------------------------------
# CLI integration


def _run_cli(argv: list[str], capsys) -> tuple[int, str]:
    rc = task_cli.main(argv)
    out = capsys.readouterr().out
    return rc, out


def test_cli_db_state_returns_payload(mind: pathlib.Path, capsys) -> None:
    _write_task_yaml(
        mind,
        "task-0900",
        title="CLI state check",
        source="jcronq/alice#900",
        tags=["github-issue:jcronq/alice#900"],
    )
    worker_markers.write_dispatch_marker(
        mind, bg_id="bg-cli0000", description="x",
        repo="jcronq/alice", gh_issue=900,
    )
    derive.derive_tasks_workers_events(mind)

    rc, out = _run_cli(
        [
            "db",
            "--db-path",
            str(_db_path(mind)),
            "state",
            "jcronq/alice#900",
        ],
        capsys,
    )
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["issue_number"] == 900
    assert any(t["id"] == "task-0900" for t in payload["tasks"])
    assert any(w["bg_id"] == "bg-cli0000" for w in payload["workers"])


def test_cli_db_workers_filter(mind: pathlib.Path, capsys) -> None:
    worker_markers.write_dispatch_marker(
        mind, bg_id="bg-worker0", description="x",
    )
    worker_markers.write_done_marker(
        mind, bg_id="bg-worker0", outcome="pr_opened",
        pr_url="http://example/pr/1",
    )
    worker_markers.write_dispatch_marker(
        mind, bg_id="bg-worker1", description="y",
    )
    worker_markers.write_done_marker(
        mind, bg_id="bg-worker1", outcome="no_change",
    )
    derive.derive_tasks_workers_events(mind)

    rc, out = _run_cli(
        [
            "db",
            "--db-path",
            str(_db_path(mind)),
            "workers",
            "--outcome",
            "pr_opened",
        ],
        capsys,
    )
    assert rc == 0
    rows = json.loads(out.strip())
    assert len(rows) == 1
    assert rows[0]["bg_id"] == "bg-worker0"


def test_cli_db_tasks_by_gh_issue(mind: pathlib.Path, capsys) -> None:
    _write_task_yaml(
        mind,
        "task-0910",
        source="jcronq/alice#910",
        tags=["github-issue:jcronq/alice#910"],
    )
    _write_task_yaml(mind, "task-0911", source="speaking")
    derive.derive_tasks_workers_events(mind)

    rc, out = _run_cli(
        [
            "db",
            "--db-path",
            str(_db_path(mind)),
            "tasks",
            "--gh-issue",
            "jcronq/alice#910",
        ],
        capsys,
    )
    assert rc == 0
    rows = json.loads(out.strip())
    assert len(rows) == 1
    assert rows[0]["id"] == "task-0910"


def test_cli_db_events_type_filter(mind: pathlib.Path, capsys) -> None:
    _append_event(
        mind, {"ts": "2026-11-06T00:00:00+00:00", "type": "workout"}
    )
    _append_event(
        mind, {"ts": "2026-11-06T00:01:00+00:00", "type": "note"}
    )
    derive.derive_tasks_workers_events(mind)

    rc, out = _run_cli(
        [
            "db",
            "--db-path",
            str(_db_path(mind)),
            "events",
            "--type",
            "workout",
        ],
        capsys,
    )
    assert rc == 0
    rows = json.loads(out.strip())
    assert len(rows) == 1
    assert rows[0]["type"] == "workout"


def test_cli_db_state_malformed_qualifier_nonzero(
    mind: pathlib.Path, capsys
) -> None:
    rc, _ = _run_cli(
        [
            "db",
            "--db-path",
            str(_db_path(mind)),
            "state",
            "not-a-qualifier",
        ],
        capsys,
    )
    assert rc != 0


# ---------------------------------------------------------------------------
# derive_runs bookkeeping


def test_derive_runs_captures_offset(mind: pathlib.Path) -> None:
    _append_event(mind, {"ts": "2026-11-06T00:00:00+00:00", "type": "x"})
    derive.derive_tasks_workers_events(mind)

    with sqlite3.connect(str(_db_path(mind))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM derive_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row["step"] == "derive_tasks_workers_events"
    assert row["events_appended"] == 1
    assert row["last_event_offset"] > 0
