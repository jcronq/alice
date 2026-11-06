"""Tests for the SM pipeline board source (#653) and its wiring into
the ``alice_queue`` section payload."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from status_site._sse import SectionBroadcaster
from status_site.server import (
    Config,
    OverviewPollers,
    section_renderers,
)
from status_site.sources import pipeline as P

NOW = 1_800_000_000.0


def _issue(n: int, *labels: str, updated: str = "2026-11-06T10:00:00Z", title: str = "") -> dict:
    return {
        "number": n,
        "title": title or f"issue {n}",
        "url": f"https://github.com/jcronq/alice/issues/{n}",
        "labels": [{"name": l} for l in labels],
        "updatedAt": updated,
    }


def _runner(by_repo: dict):
    """Fake ``gh`` runner: value is a list (JSON stdout) or an Exception-ish
    sentinel string meaning non-zero exit."""

    def run(cmd):
        repo = cmd[cmd.index("--repo") + 1]
        val = by_repo[repo]
        if isinstance(val, str):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=val)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(val), stderr="")

    return run


# ─── classify / fetch ───────────────────────────────────────────────


def test_classify_includes_each_pipeline_state():
    for st in P.PIPELINE_STATES:
        assert P.classify_issue(_issue(1, f"sm:{st}", "art:code")) == (st, "code")


@pytest.mark.parametrize(
    "excluded", ["sm:draft", "sm:done", "sm:closed", "sm:rejected", "sm:blocked"]
)
def test_classify_excludes_terminal_even_with_stale_active_label(excluded):
    assert P.classify_issue(_issue(1, "sm:building", excluded)) is None


def test_classify_ignores_non_sm_issue_and_picks_most_advanced():
    assert P.classify_issue(_issue(1, "bug")) is None
    assert P.classify_issue(_issue(1, "sm:designing", "sm:building"))[0] == "building"


def test_fetch_sorts_by_pipeline_order_then_number():
    issues = [
        _issue(5, "sm:reviewing"),
        _issue(3, "sm:selected"),
        _issue(9, "sm:designing"),
        _issue(2, "sm:designing"),
        _issue(7, "sm:draft"),
        _issue(8),
    ]
    rows, failed = P.fetch_issues(["jcronq/alice"], runner=_runner({"jcronq/alice": issues}))
    assert failed == []
    assert [(r["state"], r["number"]) for r in rows] == [
        ("selected", 3),
        ("designing", 2),
        ("designing", 9),
        ("reviewing", 5),
    ]


def test_gh_failure_for_one_repo_does_not_poison_others():
    rows, failed = P.fetch_issues(
        ["a/bad", "jcronq/alice"],
        runner=_runner({"a/bad": "boom", "jcronq/alice": [_issue(1, "sm:building")]}),
    )
    assert failed == ["a/bad"]
    assert [r["number"] for r in rows] == [1]


def test_gh_garbage_output_counts_as_failure():
    def run(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    rows, failed = P.fetch_issues(["jcronq/alice"], runner=run)
    assert rows == [] and failed == ["jcronq/alice"]


def test_issue_rows_age_math_and_missing_timestamp():
    rows, _ = P.fetch_issues(
        ["jcronq/alice"],
        runner=_runner(
            {
                "jcronq/alice": [
                    _issue(1, "sm:building", updated="2026-11-06T10:00:00Z"),
                    _issue(2, "sm:building", updated=""),
                ]
            }
        ),
    )
    base = rows[0]["updated_epoch"]
    out = P.issue_rows(rows, now=base + 90)
    assert out[0]["age_seconds"] == pytest.approx(90)
    assert out[1]["age_seconds"] is None
    assert out[0]["link"].endswith("/issues/1")


# ─── tasks ──────────────────────────────────────────────────────────


def _task(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    f = d / "task.yaml"
    f.write_text(body)
    return f


def test_scan_tasks_skips_terminal_and_handles_missing_frontmatter(tmp_path):
    _task(tmp_path, "task-0001", 'id: task-0001\ntitle: "old"\nstatus: done\n')
    _task(tmp_path, "task-0002", 'id: task-0002\ntitle: "rej"\nstatus: rejected\n')
    _task(
        tmp_path,
        "task-0003",
        'id: task-0003\ntitle: "Live one"\nstatus: selected\n'
        'updated: "2026-11-06T10:00:00+00:00"\ntags: [a, b]\n',
    )
    bare = _task(tmp_path, "task-0004", "garbage with no keys\n")
    rows = P.scan_tasks(tmp_path, now=time.time())
    by_slug = {r["slug"]: r for r in rows}
    assert set(by_slug) == {"task-0003", "task-0004"}
    assert by_slug["task-0003"]["title"] == "Live one"
    assert by_slug["task-0003"]["state"] == "selected"
    # Missing frontmatter → mtime fallback, state "unknown", no crash.
    assert by_slug["task-0004"]["state"] == "unknown"
    assert 0 <= by_slug["task-0004"]["age_seconds"] < 60
    assert bare.exists()


def test_scan_tasks_missing_dir(tmp_path):
    assert P.scan_tasks(tmp_path / "nope", now=NOW) == []


def test_render_markdown_minimal():
    assert P.render_markdown([], [], []) == "*(queue clear)*"
    assert P.render_markdown([{}, {}], [], [{}]).startswith("2 items in pipeline")


# ─── integration through the broadcaster ───────────────────────────


def test_alice_queue_payload_has_three_grouped_lists(tmp_path, monkeypatch):
    mind = tmp_path / "mind"
    _task(mind / "inner" / "tasks", "task-0010", 'id: task-0010\ntitle: "T"\nstatus: building\n')
    monkeypatch.setenv("ALICE_MIND", str(mind))

    status = tmp_path / "status"
    status.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    for i, slug in enumerate(("one", "two")):
        (surface / f"2026-11-06T10-0{i}-00-{slug}.md").write_text(
            "---\ntype: status-chat\nsource: status-site\n---\nbody\n"
        )

    cfg = Config(
        status_dir=status,
        surface_dir=surface,
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    cfg.pollers = OverviewPollers(cfg)
    # One issue per pipeline state, fed in scrambled order.
    issues = [_issue(100 + i, f"sm:{s}", "art:code") for i, s in enumerate(P.PIPELINE_STATES)]
    issues.reverse()
    rows, failed = P.fetch_issues(["jcronq/alice"], runner=_runner({"jcronq/alice": issues}))
    cfg.pollers._set("pipeline", {"issues": rows, "failed": failed})

    b = SectionBroadcaster(section_renderers(cfg), tick_seconds=0.05)
    try:
        data = b.snapshot()["sections"]["alice_queue"]
    finally:
        b.stop.set()

    assert [r["state"] for r in data["pipeline"]] == list(P.PIPELINE_STATES)
    assert all(r["art"] == "code" and r["link"] for r in data["pipeline"])
    assert len(data["rows"]) == 2  # surfaces keep the pre-#653 key
    assert [t["slug"] for t in data["tasks"]] == ["task-0010"]
    assert data["pipeline_failed"] == []
    assert data["markdown"].startswith("6 items in pipeline")


def test_alice_queue_without_pollers_still_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    cfg = Config(
        status_dir=tmp_path,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    data = section_renderers(cfg)["alice_queue"]()
    assert data["pipeline"] == [] and data["tasks"] == [] and data["rows"] == []
    assert data["markdown"] == "*(queue clear)*"


# ─── static assets ─────────────────────────────────────────────────


def test_shell_uses_pipeline_board_and_state_pills_are_distinct():
    static = Path(__file__).resolve().parent.parent / "src" / "status_site" / "static"
    html = (static / "index.html").read_text()
    css = (static / "design-tokens.css").read_text()
    assert "PipelineBoard" in html and "AliceQueueList" not in html
    for heading in ("In SM pipeline", "Surfaces", "Tasks"):
        assert heading in html
    for cls in ("sm-designing", "sm-building", "sm-reviewing"):
        assert f".fx-pill.{cls}" in css
