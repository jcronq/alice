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
    (p.inner / "canvas" / "authored-deck.md").write_text("# Hand authored\n")
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
    (p.inner / "canvas" / "exp-same-slug.md").write_text(
        "# Authored override\nbody\n"
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
    """An unflagged research note must NOT be readable via /canvas/<slug>
    even if the slug matches — path-traversal-style protection so the
    canvas pane can't be used to read arbitrary vault content."""
    p = _paths(tmp_path)
    _make_research(p, "2026-05-11-private", "---\ntitle: Private\n---\n# X\n")
    out = sources.read_canvas(p.inner, "2026-05-11-private", p.mind_dir)
    assert out is None


def test_canvas_wins_over_research_on_slug_collision(tmp_path):
    """Same precedence rule as the canvas/experiment collision."""
    p = _paths(tmp_path)
    (p.inner / "canvas").mkdir(parents=True)
    (p.inner / "canvas" / "shared.md").write_text("# Canvas wins\n")
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
