"""Tests for the Running now source — PR #2 rewrite.

Design: ``cortex-memory/designs/2026-11-24-subagent-driven-running-now.md`` §3.7.

The pre-PR-#2 shape (worktrees + ``ps`` + build-log scraping) is gone.
The new source joins SM tasks in ``status=building`` against the
start/stop log pair the hook layer writes:

* ``/state/worker/alice-task-dispatch.jsonl`` — PreToolUse Agent hook
  + steerable-dispatch helper.
* ``/state/worker/alice-subagent-stop.jsonl`` — SubagentStop hook,
  session-end reaper, orphan reaper.

The tests here cover every branch of that join: zero-tasks, active,
idle, multi-agent collapse, filtered subagent types, rotation
boundaries, source/tag derivation, and the ``steerable``/``steerable_endpoint``
plumbing added in §7.5.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from status_site.sources import running_now as rn


# ─── Low-level jsonl reader ─────────────────────────────────────────


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_read_start_log_rotation_both_files(tmp_path):
    """§3.7 rotation case — a stale start across the rotation boundary resolves."""
    primary = tmp_path / "alice-task-dispatch.jsonl"
    rotation = tmp_path / "alice-task-dispatch.jsonl.1"
    _write_jsonl(rotation, [{"tool_call_id": "old-1", "task_id": "task-0001"}])
    _write_jsonl(primary, [{"tool_call_id": "new-1", "task_id": "task-0002"}])
    rows = rn.read_start_log(primary)
    tcids = [r["tool_call_id"] for r in rows]
    assert "old-1" in tcids and "new-1" in tcids
    # Rotation file comes first so append order stays monotonic.
    assert tcids.index("old-1") < tcids.index("new-1")


def test_read_stop_log_tolerates_missing_file(tmp_path):
    """Non-existent log must return empty, not raise — covers first boot."""
    assert rn.read_stop_log(tmp_path / "nope.jsonl") == []


def test_read_jsonl_skips_malformed_lines(tmp_path):
    """One bad line can't poison the whole file (jsonl is append-only)."""
    primary = tmp_path / "x.jsonl"
    primary.write_text('{"ok": 1}\nnot json\n{"ok": 2}\n')
    rows = rn.read_start_log(primary)
    assert rows == [{"ok": 1}, {"ok": 2}]


# ─── SmTask parser ──────────────────────────────────────────────────


def test_parse_sm_task_record_full_shape():
    rec = {
        "id": "task-1025",
        "title": "Bug-tile approve",
        "status": "building",
        "updated": "2026-11-24T14:00:00+00:00",
        "source": "jcronq/alice#777",
        "tags": ["auto-registered", "status-site"],
    }
    t = rn._parse_sm_task_record(rec)
    assert t is not None
    assert t.id == "task-1025"
    assert t.source == "jcronq/alice#777"
    assert t.tags == ("auto-registered", "status-site")


def test_parse_sm_task_record_requires_id():
    assert rn._parse_sm_task_record({"title": "no id"}) is None


def test_parse_sm_task_record_accepts_csv_tags():
    """Fallback yaml reader may hand back a string for ``tags``."""
    t = rn._parse_sm_task_record({"id": "task-1", "tags": "a,b,c"})
    assert t is not None
    assert t.tags == ("a", "b", "c")


# ─── read_sm_tasks_building ─────────────────────────────────────────


def _stub_runner(stdout: str, returncode: int = 0):
    def _run(_cmd):
        return subprocess.CompletedProcess(
            args=_cmd, returncode=returncode, stdout=stdout, stderr=""
        )
    return _run


def test_read_sm_tasks_building_from_cli_ndjson():
    stdout = "\n".join([
        json.dumps({"id": "task-1", "title": "a", "status": "building"}),
        json.dumps({"id": "task-2", "title": "b", "status": "building"}),
        "",
    ])
    tasks = rn.read_sm_tasks_building(runner=_stub_runner(stdout))
    assert [t.id for t in tasks] == ["task-1", "task-2"]


def test_read_sm_tasks_building_filters_non_building():
    stdout = "\n".join([
        json.dumps({"id": "task-1", "status": "building"}),
        json.dumps({"id": "task-2", "status": "done"}),  # ignored
    ])
    tasks = rn.read_sm_tasks_building(runner=_stub_runner(stdout))
    assert [t.id for t in tasks] == ["task-1"]


def test_read_sm_tasks_building_falls_back_to_yaml(tmp_path):
    """CLI unavailable → read inner/tasks/task-*/task.yaml directly."""
    task_dir = tmp_path / "task-1234"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        "id: task-1234\n"
        "title: W40k sim\n"
        "status: building\n"
        "updated: '2026-11-24T10:00:00+00:00'\n"
        "tags:\n"
        "- auto-registered\n"
    )
    tasks = rn.read_sm_tasks_building(
        runner=_stub_runner("", returncode=2),
        tasks_dir=tmp_path,
    )
    assert len(tasks) == 1
    assert tasks[0].id == "task-1234"
    assert tasks[0].title == "W40k sim"
    assert "auto-registered" in tasks[0].tags


# ─── open_dispatches_by_task — the join core ────────────────────────


def _start(task_id, tcid="t1", session_name=None, steerable=False, ts=1000.0):
    return {
        "ts": ts,
        "tool_call_id": tcid,
        "task_id": task_id,
        "session_id": "sess-1",
        "subagent_type": "claude",
        "description": "x",
        "session_name": session_name,
        "steerable": steerable,
    }


def _stop(tcid):
    return {"event": "subagent_stop", "tool_call_id": tcid}


def test_join_open_dispatches_drops_closed():
    starts = [_start("task-1", tcid="a"), _start("task-2", tcid="b")]
    stops = [_stop("a")]
    out = rn.open_dispatches_by_task(starts, stops)
    assert "task-1" not in out
    assert out["task-2"][0].tool_call_id == "b"


def test_join_drops_rows_without_task_id():
    """§1.3: no task_id → filtered-subagent dispatch, not Running-now."""
    starts = [_start(None, tcid="a"), _start("task-2", tcid="b")]
    out = rn.open_dispatches_by_task(starts, [])
    assert list(out.keys()) == ["task-2"]


def test_join_collapses_multi_agent_rows():
    """OQ1: multiple subagents on one task_id → collapse into one group."""
    starts = [
        _start("task-9", tcid="a", ts=1000.0),
        _start("task-9", tcid="b", ts=2000.0),
    ]
    out = rn.open_dispatches_by_task(starts, [])
    assert len(out["task-9"]) == 2


def test_join_dedups_tool_call_id_across_rotation():
    """A start that appears in both rotation files only counts once."""
    starts = [
        _start("task-9", tcid="dupe"),
        _start("task-9", tcid="dupe"),
    ]
    out = rn.open_dispatches_by_task(starts, [])
    assert len(out["task-9"]) == 1


# ─── build_rows — shape / state / steerable_endpoint ────────────────


def _task(id, title="t", status="building", updated="2026-11-24T10:00:00+00:00",
          source="", tags=()):
    return rn.SmTask(id=id, title=title, status=status,
                     updated=updated, source=source, tags=tags)


def test_build_rows_zero_tasks_empty_result():
    assert rn.build_rows([], {}, now=1000.0) == []


def test_build_rows_active_shape():
    tasks = [_task("task-1", title="A")]
    opens = rn.open_dispatches_by_task(
        [_start("task-1", tcid="a", ts=900.0)], []
    )
    rows = rn.build_rows(tasks, opens, now=1000.0)
    assert len(rows) == 1
    assert rows[0].state == "active"
    assert rows[0].agent_count == 1
    assert rows[0].elapsed  # non-empty


def test_build_rows_idle_shape():
    tasks = [_task("task-1", title="A",
                   updated="2026-11-24T09:00:00+00:00")]
    rows = rn.build_rows(tasks, {}, now=1000.0)
    assert len(rows) == 1
    assert rows[0].state == "idle"
    assert rows[0].agent_count == 0


def test_build_rows_steerable_endpoint():
    """§7.5 / §7.6 — steerable row carries session_name as the endpoint."""
    tasks = [_task("task-1025")]
    opens = rn.open_dispatches_by_task(
        [_start("task-1025", tcid="a",
                session_name="task-1025", steerable=True)],
        [],
    )
    rows = rn.build_rows(tasks, opens, now=1000.0)
    assert rows[0].steerable_endpoint == "task-1025"


def test_build_rows_non_steerable_endpoint_is_none():
    tasks = [_task("task-1025")]
    opens = rn.open_dispatches_by_task(
        [_start("task-1025", tcid="a")], []
    )
    rows = rn.build_rows(tasks, opens, now=1000.0)
    assert rows[0].steerable_endpoint is None


def test_build_rows_issue_and_repo_from_source():
    """§3.4 — ``source: owner/repo#N`` → (issue, repo)."""
    tasks = [_task("task-1", source="jcronq/alice#638")]
    rows = rn.build_rows(tasks, {}, now=1000.0)
    assert rows[0].issue == "638"
    assert rows[0].repo == "jcronq/alice"


def test_build_rows_issue_from_tag():
    tasks = [_task("task-1",
                   tags=("github-issue:jcronq/cozyhem-engine#116",))]
    rows = rn.build_rows(tasks, {}, now=1000.0)
    assert rows[0].issue == "116"
    assert rows[0].repo == "jcronq/cozyhem-engine"


def test_build_rows_no_issue_when_neither_present():
    tasks = [_task("task-1", tags=("random-tag",))]
    rows = rn.build_rows(tasks, {}, now=1000.0)
    assert rows[0].issue == ""
    assert rows[0].repo == ""


# ─── collect() end-to-end ────────────────────────────────────────────


def test_collect_end_to_end(tmp_path):
    """Full pipeline: SM CLI stub + start log + stop log → rows."""
    start_log = tmp_path / "start.jsonl"
    stop_log = tmp_path / "stop.jsonl"
    _write_jsonl(start_log, [_start("task-1",
                                    tcid="a",
                                    ts=time.time() - 60)])
    _write_jsonl(stop_log, [])
    cli_stdout = json.dumps({"id": "task-1", "title": "T1",
                             "status": "building"})
    rows = rn.collect(
        start_log=start_log,
        stop_log=stop_log,
        task_bin="/nope",
        runner=_stub_runner(cli_stdout),
    )
    assert len(rows) == 1
    assert rows[0].task_id == "task-1"
    assert rows[0].state == "active"


def test_collect_hide_idle(tmp_path):
    cli_stdout = "\n".join([
        json.dumps({"id": "task-1", "status": "building"}),
        json.dumps({"id": "task-2", "status": "building"}),
    ])
    start_log = tmp_path / "start.jsonl"
    stop_log = tmp_path / "stop.jsonl"
    _write_jsonl(start_log, [_start("task-1", tcid="a")])
    _write_jsonl(stop_log, [])
    rows = rn.collect(
        start_log=start_log,
        stop_log=stop_log,
        task_bin="/nope",
        runner=_stub_runner(cli_stdout),
        hide_idle=True,
    )
    # Only task-1 is active; task-2 is idle → hidden.
    assert [r.task_id for r in rows] == ["task-1"]


def test_collect_sort_active_before_idle(tmp_path):
    cli_stdout = "\n".join([
        json.dumps({"id": "task-a", "title": "A", "status": "building"}),
        json.dumps({"id": "task-b", "title": "B", "status": "building"}),
    ])
    start_log = tmp_path / "start.jsonl"
    stop_log = tmp_path / "stop.jsonl"
    # task-b has an open dispatch; task-a is idle.
    _write_jsonl(start_log, [_start("task-b", tcid="b")])
    _write_jsonl(stop_log, [])
    rows = rn.collect(
        start_log=start_log,
        stop_log=stop_log,
        task_bin="/nope",
        runner=_stub_runner(cli_stdout),
    )
    assert rows[0].task_id == "task-b"  # active first
    assert rows[1].task_id == "task-a"  # idle


# ─── render markdown ────────────────────────────────────────────────


def test_render_empty_shows_nothing_and_footer():
    out = rn.render([], now=datetime(2026, 11, 24, tzinfo=timezone.utc).astimezone())
    assert "nothing building" in out
    assert "last_rendered" in out
    assert f"source: {rn.SOURCE_NAME}" in out
    assert rn.SOURCE_NAME == "subagent-events"


def test_render_active_row_includes_task_label_and_count():
    row = rn.RunningRow(
        task_id="task-1025",
        title="Bug-tile approve",
        state="active",
        agent_count=1,
        elapsed="2m",
        issue="",
        repo="",
        pr_url="",
        steerable_endpoint="task-1025",
        agents=[],
    )
    out = rn.render([row])
    assert "task-1025" in out
    assert "Bug-tile approve" in out
    assert "active" in out
    assert "📡" in out  # steer affordance on steerable rows


def test_render_idle_row_renders_without_steer_affordance():
    row = rn.RunningRow(
        task_id="task-1041",
        title="Stuck in building",
        state="idle",
        agent_count=0,
        elapsed="32m",
        issue="",
        repo="",
        pr_url="",
        steerable_endpoint=None,
        agents=[],
    )
    out = rn.render([row])
    assert "idle" in out
    assert "no agent" in out
    assert "📡" not in out
