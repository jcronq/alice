"""Tests for the /api/runs/{run_id} detail endpoint (#126).

Originally the endpoint only consulted ``aggregators.group_runs``, which
yields thinking-wake and speaking-turn runs (plus finished SM spawns).
Live SM-dispatcher spawns weren't visible, so clicking a row in /runs
that was still in flight returned 404.

The fix consults both ``group_runs`` and ``group_sm_runs`` and, for
sm_spawn matches, synthesises an event trace from the on-disk spawn dir
(prompt.txt / stdout.log / stderr.log) since SM workers don't emit
structured events.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from viewer import sources
from viewer.main import create_app
from viewer.settings import Paths


def _paths(tmp_path: pathlib.Path) -> Paths:
    mind = tmp_path / "mind"
    state = tmp_path / "state"
    (mind / "inner" / "state").mkdir(parents=True)
    state.mkdir()
    return Paths(
        thinking_log=state / "thinking.log",
        speaking_log=state / "speaking.log",
        turn_log=mind / "inner" / "state" / "speaking-turns.jsonl",
        mind_dir=mind,
        state_dir=state,
    )


def _finished_spawn(
    p: Paths,
    name: str,
    *,
    art_label: str = "art:code",
    prompt: str | None = None,
    stdout: str = "",
    stderr: str = "",
) -> pathlib.Path:
    d = p.state_dir / "sm-dispatcher-spawns" / ".finished" / name
    d.mkdir(parents=True)
    body = prompt if prompt is not None else (
        "You are a code-worker agent ...\n"
        f"Artifact type: {art_label}\n"
    )
    (d / "prompt.txt").write_text(body)
    if stdout:
        (d / "stdout.log").write_text(stdout)
    if stderr:
        (d / "stderr.log").write_text(stderr)
    return d


def _live_spawn(
    p: Paths,
    name: str,
    *,
    art_label: str = "art:research",
    stdout: str = "",
    stderr: str = "",
) -> pathlib.Path:
    d = p.state_dir / "sm-dispatcher-spawns" / name
    d.mkdir(parents=True)
    (d / "pidfile").write_text(str(os.getpid()))
    (d / "prompt.txt").write_text(
        "You are a research-worker agent ...\n"
        f"Artifact type: {art_label}\n"
    )
    if stdout:
        (d / "stdout.log").write_text(stdout)
    if stderr:
        (d / "stderr.log").write_text(stderr)
    return d


@pytest.fixture
def paths(tmp_path):
    return _paths(tmp_path)


@pytest.fixture
def client(paths):
    return TestClient(create_app(paths=paths))


# ---------------------------------------------------------------------------
# sources helpers


def test_find_sm_spawn_dir_prefers_finished(paths):
    """Reaped spawns live under ``.finished/`` — that's the more common
    history-view path so it's checked first."""
    finished = _finished_spawn(paths, "spawn-1-100")
    found = sources._find_sm_spawn_dir(paths, "spawn-1-100")
    assert found == finished


def test_find_sm_spawn_dir_falls_back_to_live(paths):
    live = _live_spawn(paths, "spawn-2-200")
    found = sources._find_sm_spawn_dir(paths, "spawn-2-200")
    assert found == live


def test_find_sm_spawn_dir_missing_returns_none(paths):
    (paths.state_dir / "sm-dispatcher-spawns").mkdir()
    assert sources._find_sm_spawn_dir(paths, "spawn-9-999") is None


def test_sm_spawn_trace_events_all_three_files(paths):
    d = _finished_spawn(
        paths,
        "spawn-3-300",
        stdout="hello world\n",
        stderr="warning: nothing happened\n",
    )
    evs = sources.sm_spawn_trace_events(d, fallback_ts=0.0)
    kinds = [e.kind for e in evs]
    assert kinds == ["sm_prompt", "sm_stdout", "sm_stderr"]
    assert all(e.hemisphere == "sm" for e in evs)
    assert all(e.correlation_id == "spawn-3-300" for e in evs)
    by_kind = {e.kind: e for e in evs}
    assert "Artifact type: art:code" in by_kind["sm_prompt"].detail["text"]
    assert by_kind["sm_stdout"].detail["text"] == "hello world\n"
    assert "warning" in by_kind["sm_stderr"].detail["text"]


def test_sm_spawn_trace_events_skips_missing_files(paths):
    """A spawn that died before writing stdout/stderr still produces a
    prompt event — missing files are silently dropped, not faked."""
    d = _finished_spawn(paths, "spawn-4-400")  # no stdout/stderr
    evs = sources.sm_spawn_trace_events(d, fallback_ts=0.0)
    assert [e.kind for e in evs] == ["sm_prompt"]


def test_sm_spawn_trace_events_truncates_huge_stdout(paths):
    """A runaway stdout shouldn't bloat the JSON response — keep the
    tail (where the failure usually is) plus a marker."""
    big = "x" * (sources._SM_SPAWN_FILE_MAX_BYTES + 5000)
    d = _finished_spawn(paths, "spawn-5-500", stdout=big)
    evs = sources.sm_spawn_trace_events(d, fallback_ts=0.0)
    by_kind = {e.kind: e for e in evs}
    txt = by_kind["sm_stdout"].detail["text"]
    assert "truncated" in txt
    # Marker line + capped payload — well under original size.
    assert len(txt) < len(big)


# ---------------------------------------------------------------------------
# /api/runs/{run_id} endpoint


def test_api_run_detail_finds_finished_sm_spawn(client, paths):
    """Finished SM spawns are in ``group_runs`` already, but the trace
    they carry is empty — the fix must synthesise one from disk."""
    _finished_spawn(
        paths,
        "spawn-126-1778600000",
        stdout="step 1\nstep 2\ndone\n",
    )
    r = client.get("/api/runs/spawn-126-1778600000")
    assert r.status_code == 200
    data = r.json()
    assert data["run"]["run_id"] == "spawn-126-1778600000"
    assert data["run"]["kind"] == "sm_spawn"
    kinds = [e["kind"] for e in data["events"]]
    assert "sm_prompt" in kinds
    assert "sm_stdout" in kinds
    # stdout body is actually in the trace
    stdout_ev = next(e for e in data["events"] if e["kind"] == "sm_stdout")
    assert "done" in stdout_ev["detail"]["text"]


def test_api_run_detail_finds_live_sm_spawn(client, paths):
    """Live SM spawns aren't in ``group_runs`` — only ``group_sm_runs``
    sees them. The 404 in #126 was exactly this case."""
    _live_spawn(
        paths,
        "spawn-127-1778600100",
        stdout="currently working...\n",
    )
    r = client.get("/api/runs/spawn-127-1778600100")
    assert r.status_code == 200
    data = r.json()
    assert data["run"]["run_id"] == "spawn-127-1778600100"
    assert data["run"]["status"] == "running"
    kinds = [e["kind"] for e in data["events"]]
    assert "sm_prompt" in kinds
    assert "sm_stdout" in kinds


def test_api_run_detail_finds_dead_unreaped_sm_spawn(client, paths):
    """Top-level spawn dir with a dead pidfile — finished but the
    dispatcher hasn't moved it to ``.finished/`` yet. Both #129 (live
    fall-through) and #141 (full trace) miss this case; without the
    fix in #143 the API 404s during the reap gap."""
    spawn_dir = paths.state_dir / "sm-dispatcher-spawns" / "spawn-143-1778600250"
    spawn_dir.mkdir(parents=True)
    # 2**31 - 1 is the max PID — reliably unused. Same trick as
    # test_viewer_running.py / test_viewer_history_sm_spawns.py.
    (spawn_dir / "pidfile").write_text(str(2**31 - 1))
    (spawn_dir / "prompt.txt").write_text(
        "You are a code-worker agent ...\nArtifact type: art:code\n"
    )
    (spawn_dir / "stdout.log").write_text("merged PR #200\n")
    r = client.get("/api/runs/spawn-143-1778600250")
    assert r.status_code == 200
    data = r.json()
    assert data["run"]["run_id"] == "spawn-143-1778600250"
    assert data["run"]["kind"] == "sm_spawn"
    assert data["run"]["status"] == "ended"
    kinds = [e["kind"] for e in data["events"]]
    assert "sm_prompt" in kinds
    assert "sm_stdout" in kinds
    stdout_ev = next(e for e in data["events"] if e["kind"] == "sm_stdout")
    assert "merged PR #200" in stdout_ev["detail"]["text"]


def test_api_run_detail_still_resolves_thinking_wake(client, paths):
    """Regression: the original ``group_runs`` lookup must keep working
    for thinking-wake runs."""
    now = time.time()
    paths.thinking_log.parent.mkdir(parents=True, exist_ok=True)
    paths.thinking_log.write_text(
        json.dumps({"event": "wake_start", "ts": now - 60, "model": "qwen-3-35b"})
        + "\n"
        + json.dumps({"event": "wake_end", "ts": now - 30})
        + "\n"
    )
    # The wake_id is the runtime-derived id from group_wakes; fetch it
    # via the list endpoint rather than guessing the format.
    listing = client.get("/api/runs").json()
    wake_ids = [r["run_id"] for r in listing if r["kind"] == "thinking-wake"]
    assert wake_ids, "expected at least one thinking-wake run in the listing"
    r = client.get(f"/api/runs/{wake_ids[0]}")
    assert r.status_code == 200
    assert r.json()["run"]["kind"] == "thinking-wake"


def test_api_run_detail_missing_returns_404(client):
    r = client.get("/api/runs/spawn-999-9999999999")
    assert r.status_code == 404
    assert r.json() == {"error": "not found"}


def test_api_run_detail_malformed_returns_404(client):
    """Garbage run_id values shouldn't 500 — just 404."""
    r = client.get("/api/runs/not-a-real-id")
    assert r.status_code == 404


def test_api_run_detail_sm_spawn_dir_missing_returns_run_no_events(client, paths):
    """If the spawn dir was hand-deleted after listing but before the
    detail fetch, we should still return the run metadata (don't 500)."""
    _finished_spawn(paths, "spawn-128-1778600200")
    # Race: dir disappears between listing and detail. Simulate by
    # removing it before the API call.
    spawn_path = paths.state_dir / "sm-dispatcher-spawns" / ".finished" / "spawn-128-1778600200"
    for child in spawn_path.iterdir():
        child.unlink()
    spawn_path.rmdir()
    r = client.get("/api/runs/spawn-128-1778600200")
    # The run is gone from group_runs too (since _history_sm_spawns
    # rescans), so this is just 404 — but it must not 500.
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Issue #137 — worker session JSONL + GH issue timeline merge


def _write_worker_session_jsonl(spawn_dir: pathlib.Path) -> None:
    """Drop a ``session.jsonl`` matching the claude-CLI shape into the
    spawn dir. Covers the four block types the parser handles."""
    records = [
        # initial user prompt
        {
            "type": "user",
            "timestamp": "2026-05-12T10:00:00.000Z",
            "message": {"role": "user", "content": "do the thing"},
        },
        # assistant thinking + text + tool_use in one record
        {
            "type": "assistant",
            "timestamp": "2026-05-12T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me grep first"},
                    {"type": "text", "text": "Checking the file."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Grep",
                        "input": {"pattern": "needle", "path": "src/"},
                    },
                ],
            },
        },
        # tool result (tool_result block packed into a user record)
        {
            "type": "user",
            "timestamp": "2026-05-12T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "is_error": False,
                        "content": [
                            {"type": "text", "text": "src/foo.py:42: needle"}
                        ],
                    }
                ],
            },
        },
        # noise that should be ignored
        {
            "type": "queue-operation",
            "timestamp": "2026-05-12T10:00:03.000Z",
        },
    ]
    (spawn_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


def test_parse_worker_session_events_emits_one_event_per_block(paths):
    """Each content block in the assistant record turns into its own
    UnifiedEvent so the trace can render thinking, reply, and tool call
    as distinct rows."""
    d = _finished_spawn(paths, "spawn-7-700")
    _write_worker_session_jsonl(d)

    evs = sources.parse_worker_session_events(d, fallback_ts=0.0)
    kinds = [e.kind for e in evs]
    # user prompt → user_message
    # assistant block → thinking + assistant_text + tool_use
    # tool_result block → result
    assert kinds == [
        "user_message",
        "thinking",
        "assistant_text",
        "tool_use",
        "result",
    ]
    by_kind = {e.kind: e for e in evs}
    assert by_kind["thinking"].detail["text"].startswith("let me grep")
    assert by_kind["assistant_text"].detail["text"].startswith("Checking")
    assert by_kind["tool_use"].detail["name"] == "Grep"
    assert by_kind["tool_use"].detail["input"]["pattern"] == "needle"
    assert "src/foo.py" in by_kind["result"].detail["text"]
    assert by_kind["result"].detail["is_error"] is False


def test_parse_worker_session_events_falls_back_to_session_id_glob(paths, tmp_path):
    """If session.jsonl wasn't copied into the spawn dir but a
    ``session_id`` file is present, the parser globs the projects dir
    to find the still-live JSONL — that's the live-spawn case."""
    d = _finished_spawn(paths, "spawn-8-800")
    sid = "deadbeef-1111-2222-3333-444444444444"
    (d / "session_id").write_text(sid)
    fake_projects = tmp_path / "fake-projects"
    project_subdir = fake_projects / "-some-cwd"
    project_subdir.mkdir(parents=True)
    (project_subdir / f"{sid}.jsonl").write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T11:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "live-only"}],
            },
        }) + "\n"
    )
    evs = sources.parse_worker_session_events(
        d, fallback_ts=0.0, projects_dir=fake_projects
    )
    assert [e.kind for e in evs] == ["assistant_text"]
    assert evs[0].detail["text"] == "live-only"


def test_parse_worker_session_events_no_session_returns_empty(paths):
    """No session_id, no session.jsonl, no projects dir → no events
    (the trace falls back to prompt/stdout/stderr only)."""
    d = _finished_spawn(paths, "spawn-9-900")
    evs = sources.parse_worker_session_events(d, fallback_ts=0.0)
    assert evs == []


def test_gh_timeline_to_events_filters_to_sm_signal(paths):
    """Only [SM] comments + sm:/art: label changes + cross-references +
    close/reopen survive the filter — random GitHub noise is dropped."""
    raw = [
        {
            "event": "labeled",
            "created_at": "2026-05-12T09:00:00Z",
            "actor": {"login": "jcronq"},
            "label": {"name": "sm:selected"},
        },
        {
            "event": "labeled",
            "created_at": "2026-05-12T09:01:00Z",
            "actor": {"login": "drive-by"},
            "label": {"name": "needs-design"},  # not sm/art → filtered
        },
        {
            "event": "commented",
            "created_at": "2026-05-12T09:02:00Z",
            "user": {"login": "jcronq"},
            "body": "[SM] dispatcher-hello task=#137 ...",
        },
        {
            "event": "commented",
            "created_at": "2026-05-12T09:03:00Z",
            "user": {"login": "alice"},
            "body": "Looks good to me!",  # not [SM] → filtered
        },
        {
            "event": "cross-referenced",
            "created_at": "2026-05-12T09:04:00Z",
            "actor": {"login": "jcronq"},
            "source": {
                "issue": {
                    "number": 999,
                    "html_url": "https://github.com/x/y/pull/999",
                    "pull_request": {"url": "..."},
                }
            },
        },
        {
            "event": "closed",
            "created_at": "2026-05-12T09:05:00Z",
            "actor": {"login": "jcronq"},
            "commit_id": "abc123",
        },
    ]
    out = sources._gh_timeline_to_events(raw, "spawn-1-1", fallback_ts=0.0)
    summaries = [e.summary for e in out]
    kinds = [e.kind for e in out]
    # 4 surviving events out of 6 (the non-sm label + chatty comment dropped).
    assert len(out) == 4
    assert kinds.count("gh_comment") == 1
    assert kinds.count("gh_event") == 3
    assert any("labeled sm:selected" in s for s in summaries)
    assert any("[SM] dispatcher-hello" in s for s in summaries)
    assert any("linked from PR #999" in s for s in summaries)
    assert any("closed by jcronq" in s for s in summaries)


def test_fetch_issue_timeline_cached_caches_within_ttl(paths):
    """Two calls within the TTL window hit the runner once."""
    sources._GH_TIMELINE_CACHE.clear()
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return json.dumps([{"event": "closed", "created_at": "2026-05-12T10:00:00Z"}])

    now = [1000.0]

    a = sources.fetch_issue_timeline_cached(
        "jcronq/alice", 137, runner=runner, now_ts=lambda: now[0]
    )
    b = sources.fetch_issue_timeline_cached(
        "jcronq/alice", 137, runner=runner, now_ts=lambda: now[0] + 30
    )
    assert a == b
    assert len(calls) == 1, "expected the second call to be served from cache"
    assert calls[0][0] == "gh"
    assert "repos/jcronq/alice/issues/137/timeline" in calls[0][-1]

    # Past the TTL the runner is consulted again.
    sources.fetch_issue_timeline_cached(
        "jcronq/alice", 137, runner=runner, now_ts=lambda: now[0] + 9999
    )
    assert len(calls) == 2


def test_fetch_issue_timeline_cached_returns_empty_on_runner_failure(paths):
    """A failing gh call must not crash the modal — degrade to no events."""
    sources._GH_TIMELINE_CACHE.clear()

    def runner(_args):
        raise RuntimeError("gh: command not found")

    out = sources.fetch_issue_timeline_cached("jcronq/alice", 137, runner=runner)
    assert out == []


def test_sm_spawn_trace_events_merges_all_three_sources_chronologically(paths):
    """The assembled trace contains the spawn-dir files, the worker
    session, and the GH timeline — sorted by timestamp."""
    d = _finished_spawn(paths, "spawn-137-1778600300", stdout="working...\n")
    _write_worker_session_jsonl(d)

    fake_timeline = [
        {
            "event": "labeled",
            "created_at": "2026-05-12T09:59:00Z",
            "actor": {"login": "jcronq"},
            "label": {"name": "sm:selected"},
        },
        {
            "event": "commented",
            "created_at": "2026-05-12T10:00:30Z",  # between assistant + result
            "user": {"login": "jcronq"},
            "body": "[SM] spawn-started task=#137 artifact=art:code",
        },
    ]

    evs = sources.sm_spawn_trace_events(
        d,
        fallback_ts=0.0,
        repo="jcronq/alice",
        issue_number=137,
        fetch_timeline=lambda _r, _n: fake_timeline,
    )
    kinds = [e.kind for e in evs]
    # Should include both gh_* events and worker session events plus
    # the file-shaped sm_prompt/sm_stdout.
    assert "sm_prompt" in kinds
    assert "sm_stdout" in kinds
    assert "thinking" in kinds
    assert "tool_use" in kinds
    assert "result" in kinds
    assert "gh_event" in kinds  # the labeled event
    assert "gh_comment" in kinds  # the [SM] spawn-started

    # Chronological sort: timestamps monotonically increase.
    timestamps = [e.ts for e in evs]
    assert timestamps == sorted(timestamps)


def test_sm_spawn_issue_number_pulls_n_from_spawn_id(paths):
    """The viewer extracts the issue number from the spawn id so the
    timeline fetch can target the right issue."""
    assert sources._sm_spawn_issue_number("spawn-137-1778600300") == 137
    assert sources._sm_spawn_issue_number("spawn-1-99.2") == 1  # post-collision suffix
    assert sources._sm_spawn_issue_number("not-a-spawn") is None
    assert sources._sm_spawn_issue_number("") is None
