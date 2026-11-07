"""Tests for the SM v2 tracked-tasks source (issue #780).

Covers:

* ``scan_tasks`` filters: terminal statuses dropped, GH-backed tasks
  (via ``tags`` / ``source`` / ``merge_ref``) deduplicated, missing
  frontmatter degrades to ``unknown`` state, bad YAML skipped
  silently.
* ``render`` produces bulleted markdown with age / slug / title /
  actor / artifact_type and a staleness footer, caps overflow.
* ``section_renderers`` wires a ``tracked_tasks`` key; the
  ``overview_splice_sections`` list splices the section between
  ``alice_queue`` and ``done_today`` with a fallback-append when the
  markers aren't present.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from status_site.server import (
    ALICE_QUEUE_END,
    ALICE_QUEUE_START,
    Config,
    DONE_TODAY_END,
    DONE_TODAY_START,
    TRACKED_TASKS_END,
    TRACKED_TASKS_START,
    overview_splice_sections,
    section_renderers,
    _splice_sections,
)
from status_site.sources import tasks as T

NOW = 1_800_000_000.0


def _task(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    f = d / "task.yaml"
    f.write_text(body)
    return f


# ─── scan_tasks ─────────────────────────────────────────────────────


def test_scan_tasks_missing_dir(tmp_path):
    assert T.scan_tasks(tmp_path / "nope", now=NOW) == []


def test_scan_tasks_skips_terminal_statuses(tmp_path):
    for i, status in enumerate(("done", "rejected", "cancelled", "closed")):
        _task(
            tmp_path,
            f"task-00{i:02d}",
            f"id: task-00{i:02d}\ntitle: t\nstatus: {status}\nactor: alice\n",
        )
    _task(
        tmp_path,
        "task-0099",
        'id: task-0099\ntitle: live\nstatus: draft\nactor: alice\n',
    )
    rows = T.scan_tasks(tmp_path, now=NOW)
    assert [r["slug"] for r in rows] == ["task-0099"]


def test_scan_tasks_dedups_gh_backed_tasks(tmp_path):
    # Three ways a task indicates a GH link; each should suppress it.
    _task(
        tmp_path,
        "task-0001",
        "id: task-0001\ntitle: via tag\nstatus: building\nactor: alice\n"
        "tags:\n- github-issue:jcronq/alice#17\n- other\n",
    )
    _task(
        tmp_path,
        "task-0002",
        "id: task-0002\ntitle: via source\nstatus: building\nactor: alice\n"
        "source: jcronq/alice#17\n",
    )
    _task(
        tmp_path,
        "task-0003",
        "id: task-0003\ntitle: via merge_ref\nstatus: building\nactor: alice\n"
        "merge_ref: https://github.com/jcronq/alice/pull/627\n"
        "tags:\n- github-issue:jcronq/alice#627\n",
    )
    _task(
        tmp_path,
        "task-0004",
        "id: task-0004\ntitle: via url in tag\nstatus: building\nactor: alice\n"
        "tags:\n- https://github.com/jcronq/alice/issues/42\n",
    )
    _task(
        tmp_path,
        "task-0005",
        "id: task-0005\ntitle: repo-less\nstatus: building\nactor: jason\n"
        "source: Jason Signal 2026-10-28\n",
    )
    rows = T.scan_tasks(tmp_path, now=NOW)
    assert [r["slug"] for r in rows] == ["task-0005"]


def test_scan_tasks_source_without_github_url_does_not_match(tmp_path):
    # A plain Signal/prose source must NOT be mistaken for a GH link.
    _task(
        tmp_path,
        "task-0010",
        "id: task-0010\ntitle: real todo\nstatus: draft\nactor: jason\n"
        "source: 'Oct 5 misstep; not github at all'\n",
    )
    rows = T.scan_tasks(tmp_path, now=NOW)
    assert len(rows) == 1 and rows[0]["slug"] == "task-0010"


def test_scan_tasks_missing_frontmatter_degrades_gracefully(tmp_path):
    bare = _task(tmp_path, "task-0007", "not even yaml\n")
    rows = T.scan_tasks(tmp_path, now=time.time())
    assert len(rows) == 1
    assert rows[0]["slug"] == "task-0007"
    assert rows[0]["state"] == "unknown"
    assert rows[0]["actor"] == ""
    assert rows[0]["artifact_type"] == ""
    assert 0 <= rows[0]["age_seconds"] < 60
    assert bare.exists()


def test_scan_tasks_sorts_oldest_first(tmp_path):
    _task(
        tmp_path,
        "task-0100",
        'id: task-0100\ntitle: recent\nstatus: draft\nactor: alice\n'
        'created: "2026-11-05T10:00:00+00:00"\n',
    )
    _task(
        tmp_path,
        "task-0101",
        'id: task-0101\ntitle: oldest\nstatus: draft\nactor: alice\n'
        'created: "2026-01-01T00:00:00+00:00"\n',
    )
    _task(
        tmp_path,
        "task-0102",
        'id: task-0102\ntitle: middle\nstatus: draft\nactor: alice\n'
        'created: "2026-10-01T00:00:00+00:00"\n',
    )
    # Now is some point in the future so every created maps to a
    # positive age.
    rows = T.scan_tasks(tmp_path, now=_iso_epoch("2026-12-01T00:00:00+00:00"))
    assert [r["slug"] for r in rows] == ["task-0101", "task-0102", "task-0100"]


def test_scan_tasks_falls_back_to_updated_then_mtime(tmp_path):
    _task(
        tmp_path,
        "task-0200",
        'id: task-0200\ntitle: updated only\nstatus: draft\nactor: alice\n'
        'updated: "2026-10-01T00:00:00+00:00"\n',
    )
    task_201 = _task(
        tmp_path,
        "task-0201",
        'id: task-0201\ntitle: no timestamps\nstatus: draft\nactor: alice\n',
    )
    # Pin ``now`` to just past task-0201's mtime so the mtime fallback
    # comes out as a small positive age regardless of wall-clock drift.
    mtime_201 = task_201.stat().st_mtime
    rows = T.scan_tasks(tmp_path, now=mtime_201 + 5)
    by_slug = {r["slug"]: r for r in rows}
    # task-0200 picks up the ``updated`` field → 25+ days in the past.
    assert by_slug["task-0200"]["age_seconds"] > 86400 * 20
    # task-0201 falls back to its own mtime (just written → ~5s).
    assert by_slug["task-0201"]["age_seconds"] < 60


def test_scan_tasks_picks_up_actor_and_artifact_type(tmp_path):
    _task(
        tmp_path,
        "task-0300",
        "id: task-0300\n"
        "title: ok\n"
        "status: selected\n"
        "actor: jason\n"
        "artifact_type: config_change\n",
    )
    rows = T.scan_tasks(tmp_path, now=NOW)
    assert rows[0]["actor"] == "jason"
    assert rows[0]["artifact_type"] == "config_change"
    assert rows[0]["state"] == "selected"


def test_scan_tasks_handles_flow_style_tags(tmp_path):
    _task(
        tmp_path,
        "task-0400",
        "id: task-0400\n"
        "title: flow style\n"
        "status: draft\n"
        "actor: alice\n"
        "tags: [github-issue:jcronq/alice#99, pipeline]\n",
    )
    assert T.scan_tasks(tmp_path, now=NOW) == []


def test_scan_tasks_ignores_non_task_dirs(tmp_path):
    (tmp_path / "index.jsonl").write_text("{}\n")
    (tmp_path / "subdir-not-task").mkdir()
    _task(
        tmp_path,
        "task-0500",
        "id: task-0500\ntitle: valid\nstatus: draft\nactor: alice\n",
    )
    rows = T.scan_tasks(tmp_path, now=NOW)
    assert [r["slug"] for r in rows] == ["task-0500"]


# ─── render ─────────────────────────────────────────────────────────


def test_render_empty_body():
    body = T.render([], now=_dt("2026-11-06T18:00:00"))
    assert body.startswith("*(no tracked tasks)*")
    assert "last_rendered: 2026-11-06T18:00:00" in body
    assert "source: sm-tasks" in body


def test_render_one_row_has_slug_title_actor_artifact_age():
    row = {
        "slug": "task-0760",
        "title": "Deliver Katie's three ebook asks once MAM re-auth is in",
        "state": "draft",
        "actor": "speaking",
        "artifact_type": "code",
        "age_seconds": 3600 * 25,  # 1d
        "link": "",
    }
    out = T.render([row], now=_dt("2026-11-06T18:00:00"))
    assert "**task-0760**" in out
    assert "Deliver Katie" in out
    assert "_(speaking · code)_" in out
    # 25 hours is >=24h → render as days.
    assert "- 1d —" in out
    assert out.strip().endswith("*")  # staleness footer


def test_render_truncates_long_titles():
    row = {
        "slug": "task-0001",
        "title": "x" * 200,
        "state": "draft",
        "actor": "alice",
        "artifact_type": "code",
        "age_seconds": 60,
        "link": "",
    }
    out = T.render([row], now=_dt("2026-11-06T18:00:00"))
    # Truncated title + ellipsis is <= TITLE_LIMIT chars.
    # Pull the title portion out of the first bullet.
    first = out.splitlines()[0]
    # shape: ``- <age> — **slug** <title> _(actor · code)_``
    assert "…" in first
    title_segment = first.split("**")[2]  # content after the slug bold
    # Strip the trailing meta and leading space
    just_title = title_segment.rstrip().rsplit("_(", 1)[0].strip()
    assert len(just_title) <= T.TITLE_LIMIT


def test_render_caps_overflow_and_adds_more_line():
    rows = [
        {
            "slug": f"task-{i:04d}",
            "title": "t",
            "state": "draft",
            "actor": "alice",
            "artifact_type": "",
            "age_seconds": float(i),
            "link": "",
        }
        for i in range(25)
    ]
    out = T.render(rows, now=_dt("2026-11-06T18:00:00"), cap=5)
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(lines) == 6  # 5 rows + overflow line
    assert lines[-1].startswith("- +20 more")
    assert "task ls" in lines[-1]


def test_render_handles_missing_actor():
    row = {
        "slug": "task-0001",
        "title": "x",
        "state": "unknown",
        "actor": "",
        "artifact_type": "",
        "age_seconds": 30,
        "link": "",
    }
    out = T.render([row], now=_dt("2026-11-06T18:00:00"))
    # Missing actor renders as "?" so meta row never collapses to empty
    # parens.
    assert "_(?)_" in out


def test_render_cap_env_override(monkeypatch):
    rows = [
        {
            "slug": f"task-{i:04d}",
            "title": "t",
            "state": "draft",
            "actor": "alice",
            "artifact_type": "",
            "age_seconds": float(i),
            "link": "",
        }
        for i in range(6)
    ]
    monkeypatch.setenv("TRACKED_TASKS_MAX", "2")
    out = T.render(rows, now=_dt("2026-11-06T18:00:00"))
    assert "- +4 more" in out


# ─── typed_rows ─────────────────────────────────────────────────────


def test_typed_rows_strips_internal_fields():
    rows = T.typed_rows(
        [
            {
                "slug": "task-0001",
                "title": "x",
                "state": "draft",
                "actor": "alice",
                "artifact_type": "code",
                "age_seconds": 30.0,
                "created_epoch": 1000.0,
                "link": "",
                "extra_nonsense": "should-not-leak",
            }
        ]
    )
    assert rows == [
        {
            "slug": "task-0001",
            "title": "x",
            "state": "draft",
            "actor": "alice",
            "artifact_type": "code",
            "age_seconds": 30.0,
            "link": "",
        }
    ]


# ─── integration: section_renderers + overview_splice_sections ─────


def _cfg(tmp_path: Path) -> Config:
    return Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )


def test_section_renderers_registers_tracked_tasks(tmp_path, monkeypatch):
    mind = tmp_path / "mind"
    _task(
        mind / "inner" / "tasks",
        "task-0010",
        "id: task-0010\ntitle: Live one\nstatus: draft\n"
        "actor: alice\nartifact_type: code\n",
    )
    monkeypatch.setenv("ALICE_MIND", str(mind))

    cfg = _cfg(tmp_path)
    cfg.surface_dir.mkdir(parents=True)
    cfg.status_dir.mkdir(parents=True)

    renderers = section_renderers(cfg)
    assert "tracked_tasks" in renderers
    data = renderers["tracked_tasks"]()
    assert [r["slug"] for r in data["rows"]] == ["task-0010"]
    assert "**task-0010**" in data["markdown"]
    assert "Live one" in data["markdown"]
    assert "source: sm-tasks" in data["markdown"]


def test_section_renderers_filters_gh_backed_from_tracked_tasks(
    tmp_path, monkeypatch
):
    mind = tmp_path / "mind"
    _task(
        mind / "inner" / "tasks",
        "task-0020",
        "id: task-0020\ntitle: GH-backed\nstatus: building\n"
        "actor: alice\nartifact_type: code\n"
        "tags:\n- github-issue:jcronq/alice#780\n",
    )
    _task(
        mind / "inner" / "tasks",
        "task-0021",
        "id: task-0021\ntitle: Vendor call\nstatus: draft\n"
        "actor: jason\nartifact_type: config_change\n",
    )
    monkeypatch.setenv("ALICE_MIND", str(mind))

    cfg = _cfg(tmp_path)
    cfg.surface_dir.mkdir(parents=True)
    cfg.status_dir.mkdir(parents=True)

    data = section_renderers(cfg)["tracked_tasks"]()
    slugs = [r["slug"] for r in data["rows"]]
    assert slugs == ["task-0021"]
    assert "task-0020" not in data["markdown"]


def test_overview_splice_sections_appends_tracked_tasks_between_alice_and_done(
    tmp_path, monkeypatch
):
    mind = tmp_path / "mind"
    _task(
        mind / "inner" / "tasks",
        "task-0100",
        "id: task-0100\ntitle: Vendor call\nstatus: draft\n"
        "actor: jason\nartifact_type: config_change\n",
    )
    monkeypatch.setenv("ALICE_MIND", str(mind))

    cfg = _cfg(tmp_path)
    cfg.surface_dir.mkdir(parents=True)
    cfg.status_dir.mkdir(parents=True)

    sections = overview_splice_sections(cfg, items=[], now=time.time())
    markers = [(s[0], s[1]) for s in sections]
    assert (ALICE_QUEUE_START, ALICE_QUEUE_END) in markers
    assert (TRACKED_TASKS_START, TRACKED_TASKS_END) in markers
    assert (DONE_TODAY_START, DONE_TODAY_END) in markers
    alice_idx = markers.index((ALICE_QUEUE_START, ALICE_QUEUE_END))
    tracked_idx = markers.index((TRACKED_TASKS_START, TRACKED_TASKS_END))
    done_idx = markers.index((DONE_TODAY_START, DONE_TODAY_END))
    assert alice_idx < tracked_idx < done_idx


def test_overview_splice_sections_falls_back_when_markers_missing(
    tmp_path, monkeypatch
):
    mind = tmp_path / "mind"
    _task(
        mind / "inner" / "tasks",
        "task-0100",
        "id: task-0100\ntitle: Vendor call\nstatus: draft\n"
        "actor: jason\nartifact_type: config_change\n",
    )
    monkeypatch.setenv("ALICE_MIND", str(mind))

    cfg = _cfg(tmp_path)
    cfg.surface_dir.mkdir(parents=True)
    cfg.status_dir.mkdir(parents=True)

    sections = overview_splice_sections(cfg, items=[], now=time.time())
    body = "# Status\n"
    out = _splice_sections(body, sections)
    assert "## Tracked tasks" in out
    assert TRACKED_TASKS_START in out
    assert TRACKED_TASKS_END in out
    assert "**task-0100**" in out


def test_overview_splice_sections_replaces_existing_markers(
    tmp_path, monkeypatch
):
    mind = tmp_path / "mind"
    _task(
        mind / "inner" / "tasks",
        "task-0101",
        "id: task-0101\ntitle: fresh scan\nstatus: draft\n"
        "actor: jason\nartifact_type: config_change\n",
    )
    monkeypatch.setenv("ALICE_MIND", str(mind))

    cfg = _cfg(tmp_path)
    cfg.surface_dir.mkdir(parents=True)
    cfg.status_dir.mkdir(parents=True)

    body = (
        "# Status\n\n"
        "## Tracked tasks\n"
        f"{TRACKED_TASKS_START}\n"
        "stale content that should be replaced\n"
        f"{TRACKED_TASKS_END}\n"
    )
    sections = overview_splice_sections(cfg, items=[], now=time.time())
    out = _splice_sections(body, sections)
    # Old content gone, new content in.
    assert "stale content" not in out
    assert "**task-0101**" in out
    # Markers themselves survive the splice.
    assert TRACKED_TASKS_START in out
    assert TRACKED_TASKS_END in out


# ─── helpers ────────────────────────────────────────────────────────


def _dt(iso: str) -> datetime:
    """Pinned local-time ``datetime`` for staleness-footer checks."""
    return datetime.fromisoformat(iso).astimezone()


def _iso_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()
