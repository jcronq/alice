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
        "labels": [{"name": lb} for lb in labels],
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
    "excluded", ["sm:done", "sm:closed", "sm:rejected"]
)
def test_classify_excludes_terminal_even_with_stale_active_label(excluded):
    # ``sm:blocked`` is intentionally NOT in this list — the pipeline
    # board now surfaces blocked work so dispatcher failures (bug #342)
    # don't vanish from the queue. See ``EXCLUDED_LABELS`` in
    # ``status_site.sources.pipeline``.
    assert P.classify_issue(_issue(1, "sm:building", excluded)) is None


def test_classify_untriaged():
    assert P.classify_issue(_issue(1, "sm:draft", "art:code")) == ("draft", "code")
    assert P.classify_issue(_issue(1, "art:code")) == ("draft", "code")
    assert P.classify_issue(_issue(1, "bug")) is None
    assert P.classify_issue(_issue(1, "sm:done", "art:code")) is None


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
        _issue(10, "art:code"),
        _issue(11, "sm:done", "art:code"),
    ]
    rows, failed = P.fetch_issues(["jcronq/alice"], runner=_runner({"jcronq/alice": issues}))
    assert failed == []
    assert [(r["state"], r["number"]) for r in rows] == [
        ("draft", 7),
        ("draft", 10),
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
    assert data["markdown"].startswith(f"{len(P.PIPELINE_STATES)} items in pipeline")


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


# ─── review-note synthesis (issue #719) ─────────────────────────────


def _dr_row(number: int, repo: str = "jcronq/alice", **over) -> dict:
    """Build the pipeline-row shape ensure_review_notes expects."""
    row = {
        "repo": repo,
        "number": number,
        "title": f"issue {number}",
        "url": f"https://github.com/{repo}/issues/{number}",
        "state": "design_review",
        "art": "code",
        "updated_epoch": None,
        "updated_at": "2026-11-06T21:00:00Z",
        "body": f"Body for #{number}\n",
    }
    row.update(over)
    return row


def test_review_slug_for_is_stable_and_safe():
    assert P.review_slug_for("jcronq/alice", 704) == "ghi-jcronq-alice-704"
    assert P.review_slug_for("ACME/Widgets", 616) == "ghi-acme-widgets-616"


def test_ensure_review_notes_writes_frontmatter_and_body(tmp_path):
    out = P.ensure_review_notes([_dr_row(704)], tmp_path)
    assert out["written"] == ["ghi-jcronq-alice-704"]
    # The file stem == the slug — read_review looks up ``<slug>.md``
    # under ``notes/`` with no additional prefix.
    p = tmp_path / "ghi-jcronq-alice-704.md"
    assert p.exists()
    text = p.read_text()
    for line in (
        "kind: decide",
        "slug: ghi-jcronq-alice-704",
        "repo: jcronq/alice",
        "issue: 704",
        "url: https://github.com/jcronq/alice/issues/704",
        "updated: 2026-11-06T21:00:00Z",
    ):
        assert line in text
    assert "Body for #704" in text


def test_ensure_review_notes_empty_body_renders_placeholder(tmp_path):
    P.ensure_review_notes([_dr_row(704, body="")], tmp_path)
    assert "_(no description)_" in (
        tmp_path / "ghi-jcronq-alice-704.md"
    ).read_text()


def test_ensure_review_notes_is_idempotent_on_unchanged_updated_at(tmp_path):
    row = _dr_row(704)
    first = P.ensure_review_notes([row], tmp_path)
    assert first["written"] == ["ghi-jcronq-alice-704"]
    p = tmp_path / "ghi-jcronq-alice-704.md"
    mtime_before = p.stat().st_mtime_ns
    second = P.ensure_review_notes([row], tmp_path)
    assert second["written"] == []
    assert p.stat().st_mtime_ns == mtime_before


def test_ensure_review_notes_rewrites_when_updated_at_changes(tmp_path):
    P.ensure_review_notes([_dr_row(704, updated_at="2026-11-06T10:00:00Z")], tmp_path)
    out = P.ensure_review_notes(
        [_dr_row(704, updated_at="2026-11-06T22:00:00Z", body="new body")], tmp_path
    )
    assert out["written"] == ["ghi-jcronq-alice-704"]
    text = (tmp_path / "ghi-jcronq-alice-704.md").read_text()
    assert "updated: 2026-11-06T22:00:00Z" in text
    assert "new body" in text


def test_ensure_review_notes_skips_rows_not_matching_filter(tmp_path):
    rows = [
        _dr_row(1, state="building"),         # wrong state
        _dr_row(2, art="study"),               # wrong art
        _dr_row(3, repo="", art="code"),       # no repo
        _dr_row(4),                            # matches
    ]
    out = P.ensure_review_notes(rows, tmp_path)
    assert out["written"] == ["ghi-jcronq-alice-4"]
    assert list(tmp_path.glob("ghi-*.md")) == [
        tmp_path / "ghi-jcronq-alice-4.md"
    ]


def test_ensure_review_notes_sweeps_stale_notes(tmp_path):
    # Seed two notes: one that matches a current row, one that does
    # not. The sweep should drop the orphan.
    P.ensure_review_notes([_dr_row(704), _dr_row(999)], tmp_path)
    assert {p.name for p in tmp_path.glob("ghi-*.md")} == {
        "ghi-jcronq-alice-704.md",
        "ghi-jcronq-alice-999.md",
    }
    out = P.ensure_review_notes([_dr_row(704)], tmp_path)
    # #999 moved off sm:design_review (or closed) → note gone.
    assert out["deleted"] == ["ghi-jcronq-alice-999"]
    assert list(tmp_path.glob("ghi-*.md")) == [
        tmp_path / "ghi-jcronq-alice-704.md"
    ]


def test_ensure_review_notes_leaves_unrelated_notes_alone(tmp_path):
    # Non-ghi-prefixed notes must never be touched by the sweep.
    other = tmp_path / "review-local-only.md"
    other.write_text("---\nkind: decide\n---\nbody\n")
    P.ensure_review_notes([], tmp_path)
    assert other.exists()


def test_fetch_issues_carries_body_and_updated_at_through():
    issue = _issue(704, "sm:design_review", "art:code")
    issue["body"] = "the body"
    rows, _ = P.fetch_issues(
        ["jcronq/alice"], runner=_runner({"jcronq/alice": [issue]})
    )
    assert rows[0]["body"] == "the body"
    assert rows[0]["updated_at"] == "2026-11-06T10:00:00Z"


def test_your_todo_tags_design_rows_with_review_slug_when_note_exists(
    tmp_path, monkeypatch
):
    """``_render_your_todo`` must set ``review_slug`` on design rows
    whose synthesised note already exists on disk (so the frontend
    can link the title to the local Review tab)."""
    from status_site.server import Config, OverviewPollers, section_renderers

    status = tmp_path / "status"
    (status / "notes").mkdir(parents=True)
    # Pre-seed the review note so _render_your_todo can find it.
    P.ensure_review_notes([_dr_row(704)], status / "notes")
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "mind"))
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    cfg.pollers = OverviewPollers(cfg)
    issue = _issue(704, "sm:design_review", "art:code")
    issue["body"] = "b"
    rows, failed = P.fetch_issues(
        ["jcronq/alice"], runner=_runner({"jcronq/alice": [issue]})
    )
    cfg.pollers._set("pipeline", {"issues": rows, "failed": failed})
    cfg.pollers._set(
        "review_queue",
        {"design": [], "pr": [], "design_failed": [], "pr_failed": []},
    )

    data = section_renderers(cfg)["your_todo"]()
    design = data["design_review"]
    assert len(design) == 1
    assert design[0]["review_slug"] == "ghi-jcronq-alice-704"


def test_your_todo_omits_review_slug_when_note_missing(tmp_path, monkeypatch):
    """Before the pipeline tick has written the note, the slug tag
    must stay off so the frontend links straight to GitHub."""
    from status_site.server import Config, OverviewPollers, section_renderers

    status = tmp_path / "status"
    (status / "notes").mkdir(parents=True)
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "mind"))
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    cfg.pollers = OverviewPollers(cfg)
    issue = _issue(999, "sm:design_review", "art:code")
    rows, failed = P.fetch_issues(
        ["jcronq/alice"], runner=_runner({"jcronq/alice": [issue]})
    )
    cfg.pollers._set("pipeline", {"issues": rows, "failed": failed})
    cfg.pollers._set(
        "review_queue",
        {"design": [], "pr": [], "design_failed": [], "pr_failed": []},
    )
    data = section_renderers(cfg)["your_todo"]()
    assert all("review_slug" not in r for r in data["design_review"])
