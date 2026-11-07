"""Tests for ``alice_speaking.schedule_state`` — the writer side of the
status-site Schedules panel bridge (issue #810, Phase 1).

We never assert on prompt contents in any serialised form: the whole
point of the sanitised dump is that prompts never leave Speaking.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alice_speaking import schedule_state as ss


# ─── path resolution ─────────────────────────────────────────────────


def test_active_crons_path_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ACTIVE_CRONS_PATH", raising=False)
    monkeypatch.setenv("ALICE_MIND", str(tmp_path))
    assert ss.active_crons_path() == tmp_path / "inner" / "state" / "active-crons.json"


def test_active_crons_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ACTIVE_CRONS_PATH", str(tmp_path / "x.json"))
    assert ss.active_crons_path() == tmp_path / "x.json"


# ─── mutators ────────────────────────────────────────────────────────


def test_record_cron_writes_sanitised_snapshot(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    state.record_cron(
        id="job-1",
        cron="*/5 * * * *",
        description="babysit PRs",
        next_run_at="2026-11-07T15:05:00Z",
    )
    snap = json.loads(state.path.read_text())
    assert snap["jobs"][0]["id"] == "job-1"
    assert snap["jobs"][0]["cron"] == "*/5 * * * *"
    assert snap["jobs"][0]["description"] == "babysit PRs"
    assert snap["jobs"][0]["next_run_at"] == "2026-11-07T15:05:00Z"
    # created_at auto-populated
    assert snap["jobs"][0]["created_at"]


def test_record_cron_overwrites_same_id(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    state.record_cron(id="job-1", cron="*/5 * * * *", description="v1")
    state.record_cron(id="job-1", cron="*/10 * * * *", description="v2")
    snap = json.loads(state.path.read_text())
    assert len(snap["jobs"]) == 1
    assert snap["jobs"][0]["cron"] == "*/10 * * * *"
    assert snap["jobs"][0]["description"] == "v2"


def test_record_cron_empty_id_rejected(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    with pytest.raises(ValueError):
        state.record_cron(id="", cron="*/5 * * * *")


def test_forget_cron(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    state.record_cron(id="job-1", cron="*/5 * * * *")
    assert state.forget_cron("job-1") is True
    assert state.forget_cron("job-1") is False
    snap = json.loads(state.path.read_text())
    assert snap["jobs"] == []


def test_update_run_tracks_outcome_and_next(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    state.record_cron(id="job-1", cron="*/5 * * * *")
    ok = state.update_run(
        "job-1",
        outcome="ok",
        at="2026-11-07T15:00:00Z",
        next_run_at="2026-11-07T15:05:00Z",
    )
    assert ok is True
    snap = json.loads(state.path.read_text())
    assert snap["jobs"][0]["last_run_at"] == "2026-11-07T15:00:00Z"
    assert snap["jobs"][0]["last_run_outcome"] == "ok"
    assert snap["jobs"][0]["next_run_at"] == "2026-11-07T15:05:00Z"


def test_update_run_unknown_job_is_noop(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    assert state.update_run("nope", outcome="ok") is False


def test_snapshot_never_leaks_prompt_even_if_dataclass_extended(tmp_path):
    """Defense in depth: even if a future contributor adds a ``prompt``
    attribute to :class:`ScheduledJob`, the sanitised() view uses an
    explicit allowlist so it still won't appear on disk."""
    state = ss.ScheduleState(path=tmp_path / "active.json")
    job = state.record_cron(id="job-1", cron="*/5 * * * *")
    # Sneakily graft a prompt onto the stored dataclass to simulate
    # the leak.
    setattr(job, "prompt", "secret-prompt")
    # Force a re-dump.
    state.dump()
    snap = json.loads(state.path.read_text())
    assert "prompt" not in snap["jobs"][0]


def test_sanitise_cron_args_strips_forbidden_keys():
    args = {
        "cron": "*/5 * * * *",
        "prompt": "secret",
        "prompt_preview": "also secret",
        "description": "ok",
    }
    cleaned = ss.sanitise_cron_args(args)
    assert cleaned == {"cron": "*/5 * * * *", "description": "ok"}


def test_clear_resets(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "active.json")
    state.record_cron(id="a", cron="* * * * *")
    state.record_cron(id="b", cron="* * * * *")
    state.clear()
    snap = json.loads(state.path.read_text())
    assert snap["jobs"] == []


def test_dump_creates_parent_dir(tmp_path):
    state = ss.ScheduleState(path=tmp_path / "nested" / "sub" / "crons.json")
    state.record_cron(id="a", cron="* * * * *")
    assert state.path.exists()
