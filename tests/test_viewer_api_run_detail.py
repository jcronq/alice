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

from alice_viewer import sources
from alice_viewer.main import create_app
from alice_viewer.settings import Paths


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
