"""Tests for the steerable_dispatch helper (design §7.4).

Covers:

* ``mint_task_row`` — SM-cascade shape, non-fatal failure modes.
* ``_build_claude_argv`` — ``--remote-control task-NNNN`` + ``--name`` present
  when steerability is requested, absent on the non-steerable fallback.
* ``dispatch_steerable_subagent`` — the ``dry_run`` path (no subprocess, no
  disk writes), the skip-list short-circuit, and the start-row writer under
  flock (via a monkeypatched DISPATCH_LOG).
* ``SteeringHandle.send`` — rejects non-steerable handles; writes a
  ``steer_requested`` row on steerable ones.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from alice_speaking.internal import steerable_dispatch as sd


# ─── mint_task_row ──────────────────────────────────────────────────


def _fake_task_cli(ret_sequence):
    """Build a fake runner that yields the next CompletedProcess per call."""
    calls = []
    outputs = iter(ret_sequence)

    def _run(argv, *, capture_output=True, text=True, timeout=None, **_kw):
        calls.append(argv)
        try:
            payload = next(outputs)
        except StopIteration:
            payload = subprocess.CompletedProcess(argv, 0, "", "")
        return payload

    return _run, calls


def test_mint_task_row_writes_cascade(monkeypatch):
    """Create + two update calls fire; returned id matches CLI output."""
    create_out = subprocess.CompletedProcess(
        [], 0, json.dumps({"id": "task-9999"}) + "\n", ""
    )
    sel_out = subprocess.CompletedProcess(
        [], 0, json.dumps({"id": "task-9999"}) + "\n", ""
    )
    bld_out = subprocess.CompletedProcess(
        [], 0, json.dumps({"id": "task-9999"}) + "\n", ""
    )
    fake, calls = _fake_task_cli([create_out, sel_out, bld_out])
    monkeypatch.setattr(subprocess, "run", fake)
    tid = sd.mint_task_row("Steer test")
    assert tid == "task-9999"
    assert len(calls) == 3
    # The first call is `create`; next two are `update ... --status`.
    assert calls[0][2] == "create"
    assert "selected" in calls[1]
    assert "building" in calls[2]


def test_mint_task_row_returns_none_on_create_failure(monkeypatch):
    """CLI error on create → None, non-fatal."""
    fake, _ = _fake_task_cli(
        [subprocess.CompletedProcess([], 2, "", "boom")]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert sd.mint_task_row("x") is None


# ─── _build_claude_argv ─────────────────────────────────────────────


def test_build_argv_includes_remote_control_when_task_id():
    argv = sd._build_claude_argv(
        session_id="abc",
        task_id="task-1025",
        name="task-1025 Fix the thing",
        worktree=None,
        model=None,
    )
    assert "--remote-control" in argv
    i = argv.index("--remote-control")
    assert argv[i + 1] == "task-1025"
    assert "--name" in argv
    assert "--print" in argv
    assert "--session-id" in argv


def test_build_argv_omits_remote_control_when_no_task_id():
    """Task cascade failed → no RC flag; session still spawnable."""
    argv = sd._build_claude_argv(
        session_id="abc",
        task_id=None,
        name="fallback",
        worktree=None,
        model=None,
    )
    assert "--remote-control" not in argv
    assert "--name" in argv


def test_build_argv_appends_model_and_worktree(tmp_path):
    argv = sd._build_claude_argv(
        session_id="abc",
        task_id="task-1",
        name="t",
        worktree=tmp_path,
        model="claude-opus-4-7",
    )
    assert "--worktree" in argv
    assert str(tmp_path) in argv
    assert "--model" in argv
    assert "claude-opus-4-7" in argv


# ─── dispatch_steerable_subagent — dry_run + skip-list ──────────────


def test_dispatch_skips_fork_subagent_type():
    h = sd.dispatch_steerable_subagent(
        task_id=None,
        prompt="x",
        subagent_type="fork",
    )
    assert h.steerable is False
    assert h.pid is None
    assert h.task_id is None


def test_dispatch_skips_explore_and_plan():
    for sub in ("Explore", "Plan", "statusline-setup", "web-fetch"):
        h = sd.dispatch_steerable_subagent(
            task_id=None,
            prompt="x",
            subagent_type=sub,
        )
        assert h.steerable is False, f"{sub} must be non-steerable"


def test_dispatch_dry_run_returns_pass_through_handle():
    h = sd.dispatch_steerable_subagent(
        task_id="task-1234",
        prompt="x",
        dry_run=True,
    )
    assert h.task_id == "task-1234"
    assert h.steerable is True
    assert h.pid is None
    assert h.session_name == "task-1234"


# ─── _append_dispatch_row + SteeringHandle.send ─────────────────────


def test_append_dispatch_row_flock_write(monkeypatch, tmp_path):
    log = tmp_path / "alice-task-dispatch.jsonl"
    monkeypatch.setattr(sd, "DISPATCH_LOG", log)
    sd._append_dispatch_row({"hello": "world"})
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert rows == [{"hello": "world"}]


def test_steering_handle_send_non_steerable_rejects():
    h = sd.SteeringHandle(
        task_id=None,
        session_id="s",
        pid=None,
        session_name=None,
        steerable=False,
    )
    res = h.send("ping")
    assert res["ok"] is False
    assert "not steerable" in res["error"]


def test_steering_handle_send_writes_steer_row(monkeypatch, tmp_path):
    log = tmp_path / "x.jsonl"
    monkeypatch.setattr(sd, "DISPATCH_LOG", log)
    h = sd.SteeringHandle(
        task_id="task-1025",
        session_id="s",
        pid=1234,
        session_name="task-1025",
        steerable=True,
    )
    res = h.send("steer me")
    assert res["ok"] is True
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert rows[0]["event"] == "steer_requested"
    assert rows[0]["task_id"] == "task-1025"
    assert rows[0]["message"] == "steer me"
