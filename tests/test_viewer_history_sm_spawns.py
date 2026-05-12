"""Tests for finished SM-dispatcher spawns on the timeline / history view.

Covers ``sources._history_sm_spawns`` (walks ``<spawn_dir>/.finished/``)
plus its wiring into ``aggregators.group_runs(events, paths=...)``.

The /running tab surfaces *live* spawns (``RunningJob(kind="sm_spawn")``);
this module covers their persistent counterpart — finished spawns
reaped by ``count_running_spawns`` into ``.finished/`` — and asserts
they appear on the unified timeline so a completed SM worker is
visible after exit (#116).
"""

from __future__ import annotations

import os
import pathlib
import time

from alice_viewer import aggregators, sources
from alice_viewer.settings import Paths


def _paths(tmp_path: pathlib.Path) -> Paths:
    return Paths(
        thinking_log=tmp_path / "thinking.log",
        speaking_log=tmp_path / "speaking.log",
        turn_log=tmp_path / "turns.jsonl",
        mind_dir=tmp_path / "mind",
        state_dir=tmp_path / "state",
    )


def _finished_spawn(
    p: Paths,
    name: str,
    *,
    art_label: str = "art:code",
    stdout: str = "",
    prompt_extra: str = "",
    mtime: float | None = None,
) -> pathlib.Path:
    """Create a reaped spawn dir under ``.finished/<name>/`` with the
    standard ``prompt.txt`` + (optional) ``stdout.log``. ``mtime`` lets
    callers backdate the dir so sort-order tests are deterministic."""
    d = p.state_dir / "sm-dispatcher-spawns" / ".finished" / name
    d.mkdir(parents=True)
    prompt = (
        "You are a code-worker agent ...\n"
        f"Artifact type: {art_label}\n"
        f"{prompt_extra}"
    )
    (d / "prompt.txt").write_text(prompt)
    if stdout:
        (d / "stdout.log").write_text(stdout)
    if mtime is not None:
        os.utime(d, (mtime, mtime))
    return d


def test_finished_spawn_parsed(tmp_path):
    p = _paths(tmp_path)
    _finished_spawn(
        p,
        "spawn-116-1778600000",
        art_label="art:code",
        stdout="first line\nlast status line\n",
    )
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.spawn_id == "spawn-116-1778600000"
    assert e.issue_number == 116
    assert e.art_label == "art:code"
    assert e.started_at == 1778600000
    assert e.outcome_hint == "last status line"
    assert e.ended_at > 0  # dir mtime


def test_active_spawn_not_in_history(tmp_path):
    """Spawns directly under ``spawn_dir/spawn-*/`` are *live* and must
    not leak into the finished/history listing — that path is the
    /running tab's territory."""
    p = _paths(tmp_path)
    active = p.state_dir / "sm-dispatcher-spawns" / "spawn-200-1778600100"
    active.mkdir(parents=True)
    (active / "pidfile").write_text(str(os.getpid()))
    (active / "prompt.txt").write_text("Artifact type: art:code\n")
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert entries == []


def test_missing_prompt_yields_art_unknown(tmp_path):
    """A dir with no ``prompt.txt`` (crash-before-write) still shows up
    in history with the fallback ``art:unknown`` label — the dir name
    alone proves it was a spawn."""
    p = _paths(tmp_path)
    d = p.state_dir / "sm-dispatcher-spawns" / ".finished" / "spawn-301-1778600200"
    d.mkdir(parents=True)
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert len(entries) == 1
    assert entries[0].art_label == "art:unknown"


def test_malformed_prompt_yields_art_unknown(tmp_path):
    """A ``prompt.txt`` that doesn't contain the ``Artifact type:`` line
    falls back to ``art:unknown`` rather than excluding the row."""
    p = _paths(tmp_path)
    d = p.state_dir / "sm-dispatcher-spawns" / ".finished" / "spawn-302-1778600300"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("totally unrelated content with no art line\n")
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert len(entries) == 1
    assert entries[0].issue_number == 302
    assert entries[0].art_label == "art:unknown"


def test_foreign_dirname_skipped(tmp_path):
    """``.finished/`` could legitimately contain non-spawn dirs (left
    over from manual debugging). Only ``spawn-<N>-<ts>`` shapes count."""
    p = _paths(tmp_path)
    junk = p.state_dir / "sm-dispatcher-spawns" / ".finished" / "not-a-spawn-dir"
    junk.mkdir(parents=True)
    (junk / "prompt.txt").write_text("Artifact type: art:code\n")
    _finished_spawn(p, "spawn-303-1778600400")
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert [e.spawn_id for e in entries] == ["spawn-303-1778600400"]


def test_collision_suffix_accepted(tmp_path):
    """``count_running_spawns`` appends ``.<n>`` on rare reap-time
    collisions (two same-second spawns for the same issue). The history
    walker must accept those dirs, not silently drop them."""
    p = _paths(tmp_path)
    _finished_spawn(p, "spawn-404-1778600500.1", art_label="art:research")
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert len(entries) == 1
    assert entries[0].spawn_id == "spawn-404-1778600500.1"
    assert entries[0].issue_number == 404
    assert entries[0].art_label == "art:research"


def test_sorted_newest_mtime_first(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _finished_spawn(p, "spawn-501-1778600600", mtime=now - 300)
    _finished_spawn(p, "spawn-502-1778600700", mtime=now - 30)
    _finished_spawn(p, "spawn-503-1778600650", mtime=now - 120)
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert [e.spawn_id for e in entries] == [
        "spawn-502-1778600700",
        "spawn-503-1778600650",
        "spawn-501-1778600600",
    ]


def test_no_finished_dir_returns_empty(tmp_path):
    p = _paths(tmp_path)
    entries = sources._history_sm_spawns(
        p.state_dir / "sm-dispatcher-spawns"
    )
    assert entries == []


# ---------------------------------------------------------------------------
# Wiring into group_runs


def test_group_runs_includes_finished_spawns_when_paths_given(tmp_path):
    """With ``paths`` supplied, finished SM spawns show up as Runs in the
    unified timeline alongside wakes / turns — kind=sm_spawn, status=ended,
    detail_url pointing at the GH issue."""
    p = _paths(tmp_path)
    _finished_spawn(
        p,
        "spawn-116-1778600800",
        art_label="art:code",
        stdout="ran 12 tests, all passing\n",
    )
    runs = aggregators.group_runs([], paths=p)
    sm_runs = [r for r in runs if r.kind == "sm_spawn"]
    assert len(sm_runs) == 1
    r = sm_runs[0]
    assert r.hemisphere == "sm"
    assert r.status == "ended"
    assert r.run_id == "spawn-116-1778600800"
    assert r.sender_name == "#116"
    assert "art:code" in r.summary
    assert "ran 12 tests" in r.summary
    assert r.detail_url == "https://github.com/jcronq/alice/issues/116"


def test_group_runs_omits_finished_spawns_when_paths_none(tmp_path):
    """Default (no paths) keeps the legacy event-only behavior so
    unrelated callers / tests don't suddenly start pulling spawn state."""
    p = _paths(tmp_path)
    _finished_spawn(p, "spawn-700-1778600900")
    runs = aggregators.group_runs([])  # paths omitted
    assert [r for r in runs if r.kind == "sm_spawn"] == []
