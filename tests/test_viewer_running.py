"""Tests for the /running tab + canvas auto-promote extension.

Covers:
- ``list_running_jobs`` correctly identifies open thinking wakes
  (wake_start without matching close), open experiments (state dir
  without completion in experiments.jsonl), and open speaking
  sub-agents (background_task_dispatch_request without matching
  complete event by handle).
- Stale-threshold cutoff drops zombies (start signal older than
  ``stale_threshold_s`` is treated as a crashed runtime).
- ``list_canvases`` + ``read_canvas`` scan both ``inner/canvas/``
  and ``cortex-memory/experiments/`` when ``mind_dir`` is provided,
  with canvas-authored decks winning slug collisions.
"""

from __future__ import annotations

import json
import pathlib
import time

import pytest

from alice_viewer import sources
from alice_viewer.settings import Paths


def _paths(tmp_path: pathlib.Path) -> Paths:
    """Build a Paths struct with all five fields rooted at tmp_path."""
    return Paths(
        thinking_log=tmp_path / "thinking.log",
        speaking_log=tmp_path / "speaking.log",
        turn_log=tmp_path / "turns.jsonl",
        mind_dir=tmp_path / "mind",
        state_dir=tmp_path / "state",
    )


def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Thinking wakes


def test_open_wake_detected(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.thinking_log,
        [{"event": "wake_start", "ts": now - 30, "model": "qwen-3-35b", "max_seconds": 120}],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.kind == "wake"
    assert j.job_id.startswith("wake-")
    assert j.elapsed_s == pytest.approx(30, abs=1)
    assert "qwen-3-35b" in j.what


def test_closed_wake_not_listed(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.thinking_log,
        [
            {"event": "wake_start", "ts": now - 60, "model": "m"},
            {"event": "wake_end", "ts": now - 5},
        ],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    wakes = [j for j in jobs if j.kind == "wake"]
    assert wakes == []


def test_wake_closed_by_timeout(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.thinking_log,
        [
            {"event": "wake_start", "ts": now - 60, "model": "m"},
            {"event": "timeout", "ts": now - 1, "max_seconds": 60},
        ],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert [j for j in jobs if j.kind == "wake"] == []


def test_stale_wake_dropped(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    # wake_start 3h ago, no close — zombie, drop.
    _write_jsonl(
        p.thinking_log,
        [{"event": "wake_start", "ts": now - 10800, "model": "m"}],
    )
    jobs = sources.list_running_jobs(p, now_ts=now, stale_threshold_s=7200)
    assert [j for j in jobs if j.kind == "wake"] == []


# ---------------------------------------------------------------------------
# Experiments


def test_open_experiment_detected(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    state_dir = p.mind_dir / "inner/state/experiments/exp-test-123"
    state_dir.mkdir(parents=True)
    (state_dir / "settings.json").write_text(
        json.dumps({"hypothesis": "Test hypothesis here"})
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    exps = [j for j in jobs if j.kind == "experiment"]
    assert len(exps) == 1
    assert exps[0].job_id == "exp-test-123"
    assert "Test hypothesis here" in exps[0].what


def test_completed_experiment_not_listed(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    state_dir = p.mind_dir / "inner/state/experiments/exp-done-456"
    state_dir.mkdir(parents=True)
    _write_jsonl(
        p.mind_dir / "inner/state/experiments.jsonl",
        [{"experiment_id": "exp-done-456", "status": "complete"}],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert [j for j in jobs if j.kind == "experiment"] == []


def test_stale_experiment_dropped(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    state_dir = p.mind_dir / "inner/state/experiments/exp-zombie-789"
    state_dir.mkdir(parents=True)
    # Backdate mtime to 3h ago.
    old = now - 10800
    import os

    os.utime(state_dir, (old, old))
    jobs = sources.list_running_jobs(p, now_ts=now, stale_threshold_s=7200)
    assert [j for j in jobs if j.kind == "experiment"] == []


# ---------------------------------------------------------------------------
# Speaking sub-agents


def test_open_subagent_detected(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.speaking_log,
        [
            {
                "event": "background_task_dispatch_request",
                "ts": now - 90,
                "handle": "bg-abc123",
                "description": "Audit retrieval pipeline",
                "principal_name": "Jason",
            }
        ],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    subs = [j for j in jobs if j.kind == "subagent"]
    assert len(subs) == 1
    assert subs[0].job_id == "bg-abc123"
    assert "Audit retrieval pipeline" in subs[0].what


def test_completed_subagent_not_listed(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.speaking_log,
        [
            {
                "event": "background_task_dispatch_request",
                "ts": now - 90,
                "handle": "bg-xyz999",
                "description": "Done already",
            },
            {
                "event": "background_task_dispatch_complete",
                "ts": now - 30,
                "handle": "bg-xyz999",
            },
        ],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert [j for j in jobs if j.kind == "subagent"] == []


def test_multiple_open_subagents(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.speaking_log,
        [
            {
                "event": "background_task_dispatch_request",
                "ts": now - 100,
                "handle": "bg-aaa111",
                "description": "First",
            },
            {
                "event": "background_task_dispatch_request",
                "ts": now - 50,
                "handle": "bg-bbb222",
                "description": "Second",
            },
        ],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    subs = sorted([j for j in jobs if j.kind == "subagent"], key=lambda j: j.job_id)
    assert [s.job_id for s in subs] == ["bg-aaa111", "bg-bbb222"]


# ---------------------------------------------------------------------------
# SM-dispatcher spawns


def _spawn_dir(p: Paths, name: str) -> pathlib.Path:
    d = p.state_dir / "sm-dispatcher-spawns" / name
    d.mkdir(parents=True)
    return d


def test_open_sm_spawn_detected(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    d = _spawn_dir(p, "spawn-107-1778600000")
    # Use our own PID — guaranteed alive for the duration of the test.
    (d / "pidfile").write_text(str(__import__("os").getpid()))
    (d / "prompt.txt").write_text(
        "You are a code-worker agent ...\n"
        "Artifact type: art:code\n\nIssue body:\n..."
    )
    (d / "stdout.log").write_text("first line\nlast status line\n")
    jobs = sources.list_running_jobs(p, now_ts=now)
    spawns = [j for j in jobs if j.kind == "sm_spawn"]
    assert len(spawns) == 1
    s = spawns[0]
    assert s.job_id == "spawn-107-1778600000"
    assert s.detail["issue_number"] == 107
    assert s.detail["art_label"] == "art:code"
    assert "#107" in s.what
    assert "last status line" in s.what


def test_sm_spawn_dead_pid_not_listed(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    d = _spawn_dir(p, "spawn-200-1778600001")
    # PID 0 never refers to a real process; os.kill(0, 0) signals the
    # current process group, so we use a high impossible PID instead.
    # 2**31 - 1 is the max PID and is reliably unused on a normal box.
    (d / "pidfile").write_text(str(2**31 - 1))
    (d / "prompt.txt").write_text("Artifact type: art:research\n")
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert [j for j in jobs if j.kind == "sm_spawn"] == []


def test_sm_spawn_missing_pidfile_skipped(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _spawn_dir(p, "spawn-300-1778600002")  # no pidfile written
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert [j for j in jobs if j.kind == "sm_spawn"] == []


def test_sm_spawn_finished_dir_skipped(tmp_path):
    """``.finished/`` holds dead-reaped spawns; the running view must
    not surface them even if a stale pidfile inside happens to match a
    live PID."""
    p = _paths(tmp_path)
    now = time.time()
    finished = p.state_dir / "sm-dispatcher-spawns" / ".finished" / "spawn-400-1"
    finished.mkdir(parents=True)
    (finished / "pidfile").write_text(str(__import__("os").getpid()))
    (finished / "prompt.txt").write_text("Artifact type: art:code\n")
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert [j for j in jobs if j.kind == "sm_spawn"] == []


def test_sm_spawn_stale_dropped(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    d = _spawn_dir(p, "spawn-500-1778500000")
    (d / "pidfile").write_text(str(__import__("os").getpid()))
    (d / "prompt.txt").write_text("Artifact type: art:code\n")
    import os as _os

    old = now - 10800  # 3h ago
    _os.utime(d, (old, old))
    jobs = sources.list_running_jobs(p, now_ts=now, stale_threshold_s=7200)
    assert [j for j in jobs if j.kind == "sm_spawn"] == []


# ---------------------------------------------------------------------------
# Sort order


def test_jobs_sorted_newest_first(tmp_path):
    p = _paths(tmp_path)
    now = time.time()
    _write_jsonl(
        p.thinking_log,
        [{"event": "wake_start", "ts": now - 200, "model": "m"}],
    )
    _write_jsonl(
        p.speaking_log,
        [
            {
                "event": "background_task_dispatch_request",
                "ts": now - 30,
                "handle": "bg-recent",
                "description": "newest",
            }
        ],
    )
    jobs = sources.list_running_jobs(p, now_ts=now)
    assert len(jobs) == 2
    assert jobs[0].kind == "subagent"  # newer started_at first
    assert jobs[1].kind == "wake"


# ---------------------------------------------------------------------------
# Canvas auto-promote


def test_list_canvases_picks_up_experiment_cards(tmp_path):
    p = _paths(tmp_path)
    (p.inner / "canvas").mkdir(parents=True)
    # Authored decks under inner/canvas/ are raw HTML now (per Jason's
    # 2026-05-20 directive). Markdown design drafts moved to /designs.
    (p.inner / "canvas" / "authored-deck.html").write_text(
        "<html><head><title>Hand authored</title></head>"
        "<body><h1>Hand authored</h1></body></html>"
    )
    exp_dir = p.mind_dir / "cortex-memory" / "experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "exp-2026-05-11-001.md").write_text(
        "# Experiment result\nbody body\n"
    )
    out = sources.list_canvases(p.inner, p.mind_dir)
    slugs_by_source = {(c["slug"], c["source"]) for c in out}
    assert ("authored-deck", "canvas") in slugs_by_source
    assert ("exp-2026-05-11-001", "experiment") in slugs_by_source


def test_list_canvases_authored_wins_on_collision(tmp_path):
    p = _paths(tmp_path)
    (p.inner / "canvas").mkdir(parents=True)
    (p.inner / "canvas" / "exp-same-slug.html").write_text(
        "<h1>Authored override</h1><p>body</p>"
    )
    exp_dir = p.mind_dir / "cortex-memory" / "experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "exp-same-slug.md").write_text("# Experiment loser\nbody\n")
    out = sources.list_canvases(p.inner, p.mind_dir)
    rows = [c for c in out if c["slug"] == "exp-same-slug"]
    assert len(rows) == 1
    assert rows[0]["source"] == "canvas"
    assert rows[0]["title"] == "Authored override"


def test_read_canvas_finds_experiment_card(tmp_path):
    p = _paths(tmp_path)
    exp_dir = p.mind_dir / "cortex-memory" / "experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "exp-readable.md").write_text(
        "---\nstatus: complete\n---\n# Readable\nbody\n"
    )
    out = sources.read_canvas(p.inner, "exp-readable", p.mind_dir)
    assert out is not None
    assert out["source"] == "experiment"
    assert out["title"] == "Readable"
    assert "body" in out["body"]


def test_read_canvas_without_mind_dir_only_scans_inner(tmp_path):
    p = _paths(tmp_path)
    exp_dir = p.mind_dir / "cortex-memory" / "experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "exp-invisible.md").write_text("# Invisible\n")
    out = sources.read_canvas(p.inner, "exp-invisible")  # no mind_dir
    assert out is None


# ---------------------------------------------------------------------------
# Research-paper bypass path (canvas_paper: true in frontmatter)


def _make_research(p, slug: str, body: str) -> None:
    rdir = p.mind_dir / "cortex-memory" / "research"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{slug}.md").write_text(body)


def test_list_canvases_includes_flagged_research(tmp_path):
    p = _paths(tmp_path)
    _make_research(
        p,
        "2026-05-11-flagged",
        "---\ncanvas_paper: true\n---\n# Flagged\nbody\n",
    )
    out = sources.list_canvases(p.inner, p.mind_dir)
    rows = [c for c in out if c["source"] == "research"]
    assert len(rows) == 1
    assert rows[0]["slug"] == "2026-05-11-flagged"
    assert rows[0]["title"] == "Flagged"


def test_list_canvases_excludes_unflagged_research(tmp_path):
    p = _paths(tmp_path)
    _make_research(p, "2026-05-11-noisy", "---\ntitle: Hidden\n---\n# Hidden\n")
    _make_research(p, "2026-05-11-noflag", "# No frontmatter\nbody\n")
    out = sources.list_canvases(p.inner, p.mind_dir)
    assert [c for c in out if c["source"] == "research"] == []


def test_list_canvases_research_flag_variants(tmp_path):
    """Accept ``true`` / ``yes`` / ``1`` (case-insensitively) as truthy;
    reject everything else. Slug regex is lowercase-only by separate
    constraint (see _CANVAS_SLUG_RE), so the truthy cases all use
    lowercase slugs."""
    p = _paths(tmp_path)
    for slug, value in [
        ("rp-true", "true"),
        ("rp-yes", "yes"),
        ("rp-one", "1"),
        ("rp-true-caps", "True"),  # value case-insensitive
    ]:
        _make_research(p, slug, f"---\ncanvas_paper: {value}\n---\n# {slug}\n")
    for slug, value in [
        ("rp-false", "false"),
        ("rp-no", "no"),
        ("rp-nope", "nope"),
    ]:
        _make_research(p, slug, f"---\ncanvas_paper: {value}\n---\n# {slug}\n")
    out = sources.list_canvases(p.inner, p.mind_dir)
    research_slugs = {c["slug"] for c in out if c["source"] == "research"}
    assert research_slugs == {"rp-true", "rp-yes", "rp-one", "rp-true-caps"}


def test_read_canvas_finds_flagged_research_paper(tmp_path):
    p = _paths(tmp_path)
    _make_research(
        p,
        "2026-05-11-readable-paper",
        "---\ncanvas_paper: true\n---\n# Paper\nbody body\n",
    )
    out = sources.read_canvas(p.inner, "2026-05-11-readable-paper", p.mind_dir)
    assert out is not None
    assert out["source"] == "research"
    assert out["title"] == "Paper"
    assert "body body" in out["body"]


def test_read_canvas_rejects_unflagged_research_paper(tmp_path):
    """``read_canvas`` itself returns None for an unflagged research
    note — preserves the strict opt-in semantics for callers that need
    them (e.g. canvas-index listing). The /canvas/<slug> route layers a
    fallback on top via ``read_research_note`` (see issue #175 tests
    below); this test only pins the function's own behaviour."""
    p = _paths(tmp_path)
    _make_research(p, "2026-05-11-private", "---\ntitle: Private\n---\n# X\n")
    out = sources.read_canvas(p.inner, "2026-05-11-private", p.mind_dir)
    assert out is None


def test_canvas_wins_over_research_on_slug_collision(tmp_path):
    """Same precedence rule as the canvas/experiment collision."""
    p = _paths(tmp_path)
    (p.inner / "canvas").mkdir(parents=True)
    (p.inner / "canvas" / "shared.html").write_text("<h1>Canvas wins</h1>")
    _make_research(
        p, "shared", "---\ncanvas_paper: true\n---\n# Research loses\n"
    )
    out = sources.list_canvases(p.inner, p.mind_dir)
    rows = [c for c in out if c["slug"] == "shared"]
    assert len(rows) == 1
    assert rows[0]["source"] == "canvas"


# ---------------------------------------------------------------------------
# Auto-detected research papers (tag / note_type signals, no explicit flag)


def test_auto_detect_via_experiment_tag(tmp_path):
    """``tags: [..., experiment, ...]`` should mark the note as
    canvas-eligible even without ``canvas_paper: true``."""
    p = _paths(tmp_path)
    _make_research(
        p,
        "auto-by-tag",
        "---\ntags: [research, retrieval, experiment, gcn]\n---\n# Auto by tag\n",
    )
    out = sources.list_canvases(p.inner, p.mind_dir)
    rows = [c for c in out if c["source"] == "research"]
    assert any(c["slug"] == "auto-by-tag" for c in rows)


def test_auto_detect_via_note_type(tmp_path):
    """``note_type: evaluation`` (or experiment/measurement/results)
    should also pass the auto-detect filter."""
    p = _paths(tmp_path)
    for slug, t in [
        ("nt-experiment", "experiment"),
        ("nt-evaluation", "evaluation"),
        ("nt-measurement", "measurement"),
        ("nt-results", "results"),
    ]:
        _make_research(p, slug, f"---\nnote_type: {t}\n---\n# {slug}\n")
    out = sources.list_canvases(p.inner, p.mind_dir)
    research_slugs = {c["slug"] for c in out if c["source"] == "research"}
    assert research_slugs >= {
        "nt-experiment",
        "nt-evaluation",
        "nt-measurement",
        "nt-results",
    }


def test_auto_detect_excludes_design_tag(tmp_path):
    """Tag ``design`` alone (no ``experiment``) doesn't qualify."""
    p = _paths(tmp_path)
    _make_research(
        p,
        "just-design",
        "---\ntags: [research, design, retrieval]\n---\n# Just design\n",
    )
    _make_research(
        p,
        "just-investigation",
        "---\nnote_type: investigation\n---\n# Investigation\n",
    )
    out = sources.list_canvases(p.inner, p.mind_dir)
    research_slugs = {c["slug"] for c in out if c["source"] == "research"}
    assert "just-design" not in research_slugs
    assert "just-investigation" not in research_slugs


def test_read_auto_detected_paper(tmp_path):
    p = _paths(tmp_path)
    _make_research(
        p,
        "auto-readable",
        "---\nnote_type: experiment\n---\n# Auto Readable\nbody\n",
    )
    out = sources.read_canvas(p.inner, "auto-readable", p.mind_dir)
    assert out is not None
    assert out["source"] == "research"
    assert out["title"] == "Auto Readable"


# ---------------------------------------------------------------------------
# read_research_note — the /canvas/{slug} fallback (issue #175)


def test_read_research_note_finds_unflagged(tmp_path):
    """An unflagged research note is readable via ``read_research_note``
    so the /canvas/<slug> route can render it as plain markdown."""
    p = _paths(tmp_path)
    _make_research(
        p,
        "2026-05-12-eks-portability-audit-phase-1",
        "---\ntitle: EKS\n---\n# Phase 1\nbody body\n",
    )
    out = sources.read_research_note(
        p.mind_dir, "2026-05-12-eks-portability-audit-phase-1"
    )
    assert out is not None
    assert out["source"] == "research"
    assert out["title"] == "Phase 1"
    assert "body body" in out["body"]


def test_read_research_note_finds_flagged(tmp_path):
    """Flagged notes also resolve via the fallback (same shape) — keeps
    the route logic simple: try canvas first, then unflagged fallback."""
    p = _paths(tmp_path)
    _make_research(
        p, "rp-flagged", "---\ncanvas_paper: true\n---\n# Flagged\nx\n"
    )
    out = sources.read_research_note(p.mind_dir, "rp-flagged")
    assert out is not None
    assert out["title"] == "Flagged"


def test_read_research_note_strips_frontmatter(tmp_path):
    p = _paths(tmp_path)
    _make_research(
        p, "rp-fm", "---\ntitle: T\n---\n# Body Title\nactual body\n"
    )
    out = sources.read_research_note(p.mind_dir, "rp-fm")
    assert out is not None
    assert "title: T" not in out["body"]
    assert "actual body" in out["body"]


def test_read_research_note_missing_slug_returns_none(tmp_path):
    p = _paths(tmp_path)
    p.mind_dir.mkdir(parents=True, exist_ok=True)
    out = sources.read_research_note(p.mind_dir, "does-not-exist")
    assert out is None


def test_read_research_note_rejects_path_traversal(tmp_path):
    """Slug regex blocks ``..`` and slashes; the resolve+prefix check
    blocks symlink escapes."""
    p = _paths(tmp_path)
    _make_research(p, "rp-real", "# Real\n")
    assert sources.read_research_note(p.mind_dir, "../etc/passwd") is None
    assert sources.read_research_note(p.mind_dir, "foo/bar") is None
    assert sources.read_research_note(p.mind_dir, "RP-UPPER") is None


# ---------------------------------------------------------------------------
# /canvas/{slug} route — fallback integration (issue #175)


def test_canvas_route_renders_unflagged_research_with_banner(tmp_path):
    """GET /canvas/<slug-of-unflagged-research-note> → 200 with banner
    and the note's markdown body."""
    from fastapi.testclient import TestClient

    from alice_viewer.main import create_app

    paths = _paths(tmp_path)
    paths.mind_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    _make_research(
        paths,
        "2026-05-12-design-doc",
        "---\ntitle: D\n---\n# Design Doc\nplain body text\n",
    )
    app = create_app(paths=paths)
    client = TestClient(app)
    r = client.get("/canvas/2026-05-12-design-doc")
    assert r.status_code == 200
    body = r.text
    assert "Design Doc" in body
    assert "plain body text" in body
    assert "rendering as plain markdown" in body


def test_canvas_route_flagged_research_has_no_banner(tmp_path):
    """Regression: flagged notes still render via the canvas-paper
    path and don't get the fallback banner."""
    from fastapi.testclient import TestClient

    from alice_viewer.main import create_app

    paths = _paths(tmp_path)
    paths.mind_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    _make_research(
        paths,
        "2026-05-12-paper",
        "---\ncanvas_paper: true\n---\n# Real Paper\nbody\n",
    )
    app = create_app(paths=paths)
    client = TestClient(app)
    r = client.get("/canvas/2026-05-12-paper")
    assert r.status_code == 200
    assert "Real Paper" in r.text
    assert "rendering as plain markdown" not in r.text


def test_canvas_route_unknown_slug_still_404s(tmp_path):
    """Regression: a slug that doesn't exist anywhere still 404s with
    the existing 'canvas not found' page."""
    from fastapi.testclient import TestClient

    from alice_viewer.main import create_app

    paths = _paths(tmp_path)
    paths.mind_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(paths=paths)
    client = TestClient(app)
    r = client.get("/canvas/2026-05-12-nope")
    assert r.status_code == 404
    assert "canvas not found" in r.text


def test_canvas_route_authored_canvas_serves_raw_html(tmp_path):
    """Regression: authored canvas decks under ``inner/canvas/`` are
    served as raw HTML (per Jason's 2026-05-20 directive). The viewer
    no longer wraps them in a markdown→slideshow template."""
    from fastapi.testclient import TestClient

    from alice_viewer.main import create_app

    paths = _paths(tmp_path)
    paths.mind_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.inner / "canvas").mkdir(parents=True, exist_ok=True)
    raw_html = (
        "<!doctype html><html><head><title>Deck</title></head>"
        "<body><h1>Hand authored slideshow</h1>"
        "<p>raw html content</p></body></html>"
    )
    (paths.inner / "canvas" / "authored-deck.html").write_text(raw_html)
    app = create_app(paths=paths)
    client = TestClient(app)
    r = client.get("/canvas/authored-deck")
    assert r.status_code == 200
    assert "Hand authored slideshow" in r.text
    assert "raw html content" in r.text
    # No paper-template chrome — the body is the file, verbatim.
    assert "rendering as plain markdown" not in r.text
