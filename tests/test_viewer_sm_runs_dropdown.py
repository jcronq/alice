"""Tests for the dedicated /runs SM-dispatcher history view (#124).

The /running tab covers all live workers; the main timeline shows only
*finished* SM spawns. /runs is the SM-only history view that stitches
live + finished into a single chronological list — its own
history-dropdown entry next to wakes / turns / interactions.

Covers:
- ``aggregators.group_sm_runs`` includes both live and finished spawns
  with the right status and detail_url.
- /runs returns only SM spawns, never thinking/speaking runs.
- The history dropdown in the page chrome contains a /runs link, and
  the link is marked active when the page is /runs.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from alice_viewer import aggregators
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
    stdout: str = "",
) -> pathlib.Path:
    d = p.state_dir / "sm-dispatcher-spawns" / ".finished" / name
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text(
        "You are a code-worker agent ...\n"
        f"Artifact type: {art_label}\n"
    )
    if stdout:
        (d / "stdout.log").write_text(stdout)
    return d


def _live_spawn(
    p: Paths,
    name: str,
    *,
    art_label: str = "art:research",
    stdout: str = "",
) -> pathlib.Path:
    """Create an active spawn dir with this process's own pid in
    ``pidfile`` so ``os.kill(pid, 0)`` liveness-checks pass."""
    d = p.state_dir / "sm-dispatcher-spawns" / name
    d.mkdir(parents=True)
    (d / "pidfile").write_text(str(os.getpid()))
    (d / "prompt.txt").write_text(
        "You are a research-worker agent ...\n"
        f"Artifact type: {art_label}\n"
    )
    if stdout:
        (d / "stdout.log").write_text(stdout)
    return d


@pytest.fixture
def paths(tmp_path):
    return _paths(tmp_path)


@pytest.fixture
def client(paths):
    return TestClient(create_app(paths=paths))


# ---------------------------------------------------------------------------
# group_sm_runs aggregator


def test_group_sm_runs_includes_finished(paths):
    _finished_spawn(
        paths,
        "spawn-124-1778600000",
        art_label="art:code",
        stdout="all good\n",
    )
    runs = aggregators.group_sm_runs(paths)
    assert len(runs) == 1
    r = runs[0]
    assert r.kind == "sm_spawn"
    assert r.hemisphere == "sm"
    assert r.status == "ended"
    assert r.end_ts is not None
    assert r.sender_name == "#124"
    assert r.detail_url == "https://github.com/jcronq/alice/issues/124"


def test_group_sm_runs_includes_live(paths):
    _live_spawn(
        paths,
        "spawn-125-1778600100",
        art_label="art:research",
    )
    # ``now_ts`` injection keeps liveness window deterministic — the
    # spawn dir's mtime is "now" at creation, so we anchor the cutoff
    # window around it.
    runs = aggregators.group_sm_runs(paths, now_ts=time.time())
    assert len(runs) == 1
    r = runs[0]
    assert r.kind == "sm_spawn"
    assert r.status == "running"
    assert r.end_ts is None
    assert r.is_running is True
    assert r.sender_name == "#125"
    assert r.detail_url == "https://github.com/jcronq/alice/issues/125"


def test_group_sm_runs_live_and_finished_together(paths):
    """Mixing both sources is the whole point of this view — verify the
    sort puts the newer start_ts first regardless of which source it
    came from."""
    _finished_spawn(
        paths,
        "spawn-126-1778600000",  # older start
        art_label="art:code",
    )
    _live_spawn(
        paths,
        "spawn-127-1778600500",  # newer start
        art_label="art:research",
    )
    runs = aggregators.group_sm_runs(paths, now_ts=time.time())
    assert len(runs) == 2
    # newest start first
    assert runs[0].run_id == "spawn-127-1778600500"
    assert runs[0].status == "running"
    assert runs[1].run_id == "spawn-126-1778600000"
    assert runs[1].status == "ended"


def test_group_sm_runs_empty_when_no_spawns(paths):
    runs = aggregators.group_sm_runs(paths)
    assert runs == []


# ---------------------------------------------------------------------------
# /runs HTML route


def test_runs_route_renders(client, paths):
    _finished_spawn(
        paths,
        "spawn-128-1778600200",
        art_label="art:code",
        stdout="ran 9 tests, all passing\n",
    )
    r = client.get("/runs")
    assert r.status_code == 200
    body = r.text
    # Header + row content
    assert "SM dispatcher runs" in body
    assert "spawn-128-1778600200" in body
    assert "#128" in body
    # The sm_spawn badge styling hook
    assert "kind-sm_spawn" in body
    # GitHub issue link present (#128 → /issues/128)
    assert "https://github.com/jcronq/alice/issues/128" in body


def test_runs_route_filters_to_sm_spawn_only(client, paths):
    """The /runs view must not surface thinking-wake / speaking-turn
    runs even if those events exist in the log — that's the timeline's
    job. The filter is implicit in ``group_sm_runs`` not consuming the
    event stream."""
    import json

    paths.thinking_log.parent.mkdir(parents=True, exist_ok=True)
    paths.thinking_log.write_text(
        json.dumps({"event": "wake_start", "ts": time.time() - 60, "model": "m"})
        + "\n"
        + json.dumps({"event": "wake_end", "ts": time.time() - 30})
        + "\n"
    )
    _finished_spawn(paths, "spawn-129-1778600300", art_label="art:code")
    r = client.get("/runs")
    assert r.status_code == 200
    body = r.text
    # SM row present
    assert "spawn-129-1778600300" in body
    # No thinking-wake kind chip in this view
    assert "kind-thinking-wake" not in body


def test_runs_dropdown_link_present_and_active(client, paths):
    r = client.get("/runs")
    assert r.status_code == 200
    body = r.text
    # The dropdown entry itself
    assert 'href="/runs"' in body
    # Active marker on the link
    assert '<a href="/runs"         class="on">runs</a>' in body
    # And the dropdown summary reflects the active page
    assert "history · runs" in body


def test_runs_dropdown_link_present_on_other_pages(client):
    """Even when not on /runs, the dropdown should list it next to
    interactions so a user can navigate over."""
    r = client.get("/wakes")
    assert r.status_code == 200
    body = r.text
    assert 'href="/runs"' in body


def test_runs_page_partial_serves_next_offset(client, paths):
    for i in range(3):
        _finished_spawn(
            paths,
            f"spawn-{200 + i}-{1778600400 + i}",
            art_label="art:code",
        )
    r = client.get("/runs/page?offset=0&limit=2")
    assert r.status_code == 200
    body = r.text
    # Two rows in this page
    assert body.count("kind-sm_spawn") == 2
    # Sentinel points back at /runs/page, not /timeline/page
    assert "/runs/page?offset=2" in body
    assert "/timeline/page" not in body


def test_runs_empty_state(client):
    r = client.get("/runs")
    assert r.status_code == 200
    assert "No SM dispatcher runs yet" in r.text
