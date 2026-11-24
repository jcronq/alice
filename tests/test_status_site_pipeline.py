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
from status_site.sources import pipeline_liveness as P_live

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


# ─── partition_by_state_bucket (issue #868) ────────────────────────


def test_partition_by_state_bucket_routes_each_state_to_right_bucket():
    """Carve the pipeline by *liveness*: in-flight vs blocked vs queued
    vs everything else. Jason's mental model: "in flight means there's
    an actual agent working it right now" — ``building`` / ``reviewing``
    only. ``blocked`` is stalled; ``selected`` / ``designed`` are queued."""
    rows = [
        {"state": "building", "number": 1},
        {"state": "reviewing", "number": 2},
        {"state": "blocked", "number": 3},
        {"state": "selected", "number": 4},
        {"state": "designed", "number": 5},
        {"state": "draft", "number": 6},
        {"state": "designing", "number": 7},
        {"state": "design_review", "number": 8},
        {"state": "", "number": 9},
    ]
    buckets = P.partition_by_state_bucket(rows)
    assert [r["number"] for r in buckets["in_flight"]] == [1, 2]
    assert [r["number"] for r in buckets["blocked"]] == [3]
    assert [r["number"] for r in buckets["queued"]] == [4, 5]
    assert [r["number"] for r in buckets["other"]] == [6, 7, 8, 9]


def test_partition_by_state_bucket_preserves_input_order_inside_bucket():
    rows = [
        {"state": "reviewing", "n": 2},
        {"state": "building", "n": 1},
        {"state": "reviewing", "n": 3},
    ]
    buckets = P.partition_by_state_bucket(rows)
    assert [r["n"] for r in buckets["in_flight"]] == [2, 1, 3]


def test_partition_by_state_bucket_empty_input_returns_empty_buckets():
    buckets = P.partition_by_state_bucket([])
    assert buckets == {"in_flight": [], "blocked": [], "queued": [], "other": []}


def test_partition_state_constants_are_disjoint():
    assert not (P.INFLIGHT_STATES & P.BLOCKED_STATES)
    assert not (P.INFLIGHT_STATES & P.QUEUED_STATES)
    assert not (P.BLOCKED_STATES & P.QUEUED_STATES)


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


# ─── in_flight vs blocked carve-out (issue #868) ───────────────────


def _patch_scan_all_live(monkeypatch, numbers):
    """Swap :meth:`LivenessScan.collect` for one that marks every
    ``numbers`` entry as live via a synthetic ``ps_output``.

    The in_flight renderer calls ``LivenessScan.collect()`` with no
    arguments; this fake returns a scan whose ``ps_output`` carries a
    ``claude --issue <N>`` line per number, which makes every row
    whose ``number`` is in ``numbers`` satisfy the process signal.
    """
    ps = "\n".join(f"claude --issue {n}" for n in numbers)
    fake = P_live.LivenessScan(
        ps_output=ps, worktrees=(), active_spawn_issues=frozenset()
    )
    monkeypatch.setattr(
        P_live.LivenessScan, "collect", classmethod(lambda cls, **kw: fake)
    )


def _mk_cfg_with_pipeline_states(tmp_path, states, monkeypatch):
    """Spin up a Config whose pollers cache has one issue per listed state.

    Returns the Config with ``pollers`` populated. Surfaces/tasks dirs
    exist but are empty — the carve-out tests focus on pipeline rows.

    Every issue number in the fixture is marked live so the liveness
    cross-check (slice 2b) is a no-op for label-only tests; tests that
    specifically exercise the liveness demotion construct their own
    scan.
    """
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "mind"))
    (tmp_path / "status").mkdir()
    (tmp_path / "surface").mkdir()
    cfg = Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    cfg.pollers = OverviewPollers(cfg)
    issues = [_issue(100 + i, f"sm:{s}", "art:code") for i, s in enumerate(states)]
    rows, failed = P.fetch_issues(["jcronq/alice"], runner=_runner({"jcronq/alice": issues}))
    cfg.pollers._set("pipeline", {"issues": rows, "failed": failed})
    _patch_scan_all_live(
        monkeypatch, [str(100 + i) for i, _ in enumerate(states)]
    )
    return cfg


def test_section_factory_registers_blocked_section(tmp_path, monkeypatch):
    """``blocked`` is a first-class section alongside ``in_flight``."""
    cfg = _mk_cfg_with_pipeline_states(tmp_path, [], monkeypatch)
    renderers = section_renderers(cfg)
    assert "blocked" in renderers
    assert "in_flight" in renderers


def test_render_blocked_renders_blocked_rows_only(tmp_path, monkeypatch):
    """Feeding a mix of states: only ``blocked`` appears in the payload."""
    cfg = _mk_cfg_with_pipeline_states(
        tmp_path, ["building", "reviewing", "blocked", "selected"], monkeypatch
    )
    data = section_renderers(cfg)["blocked"]()
    assert data["markdown"] == "1 blocked"
    assert [r["state"] for r in data["rows"]] == ["blocked"]
    assert data["pipeline_failed"] == []
    # Row carries the standard shape: repo, number, title, link, age.
    row = data["rows"][0]
    assert row["repo"] == "jcronq/alice"
    assert row["number"] and row["link"] and row["title"]


def test_render_blocked_empty_state(tmp_path, monkeypatch):
    """No blocked rows → placeholder markdown + empty rows list."""
    cfg = _mk_cfg_with_pipeline_states(
        tmp_path, ["building", "reviewing"], monkeypatch
    )
    data = section_renderers(cfg)["blocked"]()
    assert data["markdown"] == "*(nothing blocked)*"
    assert data["rows"] == []


def test_render_in_flight_excludes_blocked_and_queued(tmp_path, monkeypatch):
    """Regression: a blocked row must NOT appear in ``in_flight.pipeline``.

    This is the whole point of issue #868 — Jason's "In flight" tier
    stopped meaning "an agent is working this" once ``sm:blocked`` rows
    started piling up there.
    """
    cfg = _mk_cfg_with_pipeline_states(
        tmp_path,
        ["building", "reviewing", "blocked", "selected", "designed"],
        monkeypatch,
    )
    data = section_renderers(cfg)["in_flight"]()
    states = sorted(r["state"] for r in data["pipeline"])
    assert states == ["building", "reviewing"]
    assert "blocked" not in {r["state"] for r in data["pipeline"]}
    # ``n`` count only counts in-flight pipeline rows (plus running /
    # surfaces / tasks — none in this fixture).
    assert data["markdown"] == "2 in flight"
    # pipeline_by_component is derived from the in-flight subset too.
    comp_states = {r["state"] for g in data["pipeline_by_component"] for r in g["rows"]}
    assert comp_states <= {"building", "reviewing"}


def test_render_in_flight_nothing_when_only_blocked_or_queued(tmp_path, monkeypatch):
    """If everything is blocked or queued, in-flight tier reads empty."""
    cfg = _mk_cfg_with_pipeline_states(
        tmp_path, ["blocked", "selected", "designed"], monkeypatch
    )
    data = section_renderers(cfg)["in_flight"]()
    assert data["pipeline"] == []
    assert data["markdown"] == "*(nothing in flight)*"
    # But the blocked tier picks it up.
    blocked = section_renderers(cfg)["blocked"]()
    assert [r["state"] for r in blocked["rows"]] == ["blocked"]


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


def test_shell_defines_blocked_tier_and_board():
    """Issue #868: the client shell separates ``blocked`` from ``in_flight``."""
    static = Path(__file__).resolve().parent.parent / "src" / "status_site" / "static"
    html = (static / "index.html").read_text()
    # Tier wired in TIERS config and section renderer dispatch.
    assert 'id: "blocked"' in html
    assert 'sections: ["blocked"]' in html
    assert "BlockedBoard" in html


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


# ─── issue #821: group by component ───────────────────────────────────


def test_issue_rows_propagates_components():
    """``issue_rows`` carries ``components`` through to the widget row."""
    cached = [
        {
            "repo": "jcronq/alice",
            "number": 790,
            "title": "design",
            "state": "selected",
            "art": "code",
            "url": "https://github.com/jcronq/alice/issues/790",
            "updated_epoch": NOW - 3600,
            "components": ["status_site", "alice_forge"],
        },
        {
            "repo": "jcronq/alice",
            "number": 802,
            "title": "pre",
            "state": "selected",
            "art": "code",
            "url": "https://github.com/jcronq/alice/issues/802",
            "updated_epoch": NOW - 60,
            # missing components → renderer defaults to pre-build
        },
    ]
    rows = P.issue_rows(cached, now=NOW)
    assert rows[0]["components"] == ["alice_forge", "status_site"]
    assert rows[1]["components"] == []


def test_group_rows_by_component_sorts_by_count_desc():
    """Buckets are sorted by count desc, then name asc for stability."""
    rows = [
        {"number": 1, "components": ["alice_forge"]},
        {"number": 2, "components": ["alice_forge", "status_site"]},
        {"number": 3, "components": ["status_site"]},
        {"number": 4, "components": ["alice_forge"]},
    ]
    groups = P.group_rows_by_component(rows)
    assert [g["component"] for g in groups] == ["alice_forge", "status_site"]
    assert groups[0]["count"] == 3  # #1, #2, #4
    assert groups[1]["count"] == 2  # #2, #3


def test_group_rows_by_component_pre_build_bucket_for_empty_components():
    """A row with no components lands in the ``pre-build`` bucket."""
    rows = [
        {"number": 1, "components": []},
        {"number": 2, "components": None},
        {"number": 3},
    ]
    groups = P.group_rows_by_component(rows)
    assert len(groups) == 1
    assert groups[0]["component"] == "pre-build"
    assert groups[0]["count"] == 3


def test_group_rows_by_component_row_appears_in_each_bucket():
    """A row with multiple components shows up in each of its buckets."""
    rows = [
        {"number": 1, "components": ["alice_forge", "status_site"]},
    ]
    groups = P.group_rows_by_component(rows)
    assert sorted(g["component"] for g in groups) == ["alice_forge", "status_site"]
    for g in groups:
        assert [r["number"] for r in g["rows"]] == [1]


def test_group_rows_by_component_empty_input_empty_output():
    assert P.group_rows_by_component([]) == []


# ─── issue #832: /api/pipeline-history + /history/<id> SPA shell ─────


@pytest.fixture
def history_site(tmp_path, monkeypatch):
    """Spin up a status_site server pointed at a seeded alice-state.db."""
    import sqlite3 as _sqlite
    import threading as _thr

    from alice_state_db.schema import apply_schema
    from status_site.server import Config, make_server

    db_path = tmp_path / "alice-state.db"
    monkeypatch.setenv("ALICE_STATE_DB_PATH", str(db_path))
    conn = _sqlite.connect(str(db_path))
    apply_schema(conn)
    conn.execute(
        "INSERT INTO items (item_id, item_type, title) "
        "VALUES ('gh:jcronq/alice#832', 'gh:issue', 'Drawer')"
    )
    conn.execute(
        "INSERT INTO state_machine (item_id, state, entered_at) "
        "VALUES ('gh:jcronq/alice#832', 'sm:building', '2026-11-07T12:00:00Z')"
    )
    conn.executemany(
        "INSERT INTO transitions "
        "(item_id, from_state, to_state, actor, reason, ts, source, source_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("gh:jcronq/alice#832", None, "sm:selected", "jason", "picked",
             "2026-11-07T09:00:00Z", "gh-comment", "r1"),
            ("gh:jcronq/alice#832", "sm:selected", "sm:designing", "alice",
             "design", "2026-11-07T10:00:00Z", "gh-comment", "r2"),
            ("gh:jcronq/alice#832", "sm:designing", "sm:building", "alice",
             "spawn", "2026-11-07T12:00:00Z", "gh-comment", "r3"),
        ],
    )
    conn.commit()
    conn.close()

    status = tmp_path / "status"
    status.mkdir()
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    srv = make_server(cfg)
    _thr.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield cfg, srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _hreq(port, method, path):
    import http.client as _http
    c = _http.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    status = r.status
    headers = dict(r.getheaders())
    body = r.read()
    c.close()
    return status, headers, body


def test_pipeline_history_endpoint_returns_transitions(history_site):
    import json as _json
    _, port = history_site
    status, _, body = _hreq(
        port, "GET",
        "/api/pipeline-history?item=" + "gh%3Ajcronq%2Falice%23832"
        + "&limit=10",
    )
    assert status == 200
    payload = _json.loads(body)
    assert payload["item_id"] == "gh:jcronq/alice#832"
    assert payload["title"] == "Drawer"
    assert payload["state"] == "sm:building"
    assert len(payload["transitions"]) == 3
    # Newest-first, tuple-shaped row.
    assert payload["transitions"][0]["to_state"] == "sm:building"
    assert set(payload["transitions"][0].keys()) == {
        "ts", "from_state", "to_state", "actor", "reason", "source",
    }


def test_pipeline_history_endpoint_404_for_unknown_item(history_site):
    _, port = history_site
    status, _, _ = _hreq(
        port, "GET",
        "/api/pipeline-history?item=" + "gh%3Anope%2Fnope%231",
    )
    assert status == 404


def test_pipeline_history_endpoint_400_for_missing_item_qs(history_site):
    _, port = history_site
    status, _, _ = _hreq(port, "GET", "/api/pipeline-history")
    assert status == 400


def test_history_path_serves_spa_shell(history_site):
    """``GET /history/<url-encoded-item-id>`` serves index.html."""
    _, port = history_site
    status, headers, body = _hreq(
        port, "GET",
        "/history/" + "gh%3Ajcronq%2Falice%23832",
    )
    assert status == 200
    ctype = headers.get("Content-Type", "")
    assert ctype.startswith("text/html")
    # Sanity-check that we got the shell, not an error page.
    assert b"Alice" in body or b"<!doctype html" in body.lower()


def test_history_path_shell_for_unknown_id_still_serves_html(history_site):
    """The SPA shell is handed back for any ``/history/*`` — the JS
    client is responsible for the API lookup + 404 rendering.
    """
    _, port = history_site
    status, headers, _ = _hreq(
        port, "GET",
        "/history/" + "gh%3Anope%2Fnope%231",
    )
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")


# ─── #858: transient-empty guard in _poll_pipeline_once ────────────


def _poll_cfg(tmp_path):
    cfg = Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    cfg.status_dir.mkdir(parents=True, exist_ok=True)
    cfg.surface_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_poll_pipeline_once_writes_cache_on_fresh_fetch(tmp_path, monkeypatch):
    cfg = _poll_cfg(tmp_path)
    pollers = OverviewPollers(cfg, pipeline_poll=0.01)
    rows = [{"repo": "jcronq/alice", "number": 1, "state": "selected", "title": "t"}]
    calls = []

    def fake_fetch(repos):
        calls.append(repos)
        return rows, []

    monkeypatch.setattr(P, "repos_sm_from_config", lambda: ["jcronq/alice"])
    monkeypatch.setattr(P, "fetch_issues", fake_fetch)
    monkeypatch.setattr(P, "ensure_review_notes", lambda *a, **kw: {"written": [], "deleted": []})

    pollers._poll_pipeline_once()
    cached = pollers.get("pipeline")
    assert cached == {"issues": rows, "failed": []}
    assert len(calls) == 1


def test_poll_pipeline_once_empty_without_prior_writes_empty(tmp_path, monkeypatch):
    cfg = _poll_cfg(tmp_path)
    pollers = OverviewPollers(cfg, pipeline_poll=0.01)

    def fake_fetch(repos):
        return [], []

    monkeypatch.setattr(P, "repos_sm_from_config", lambda: ["jcronq/alice"])
    monkeypatch.setattr(P, "fetch_issues", fake_fetch)
    monkeypatch.setattr(P, "ensure_review_notes", lambda *a, **kw: {"written": [], "deleted": []})

    # No prior cache entry — the first poll must still write (even if
    # empty) so renderers get the empty-marker payload and don't
    # loop forever waiting for data.
    pollers._poll_pipeline_once()
    cached = pollers.get("pipeline")
    assert cached == {"issues": [], "failed": []}


def test_poll_pipeline_once_transient_empty_after_full_keeps_prior(
    tmp_path, monkeypatch
):
    """Regression guard for #858.

    When ``fetch_issues`` returns ``([], [])`` (sqlite3.Error swallowed
    inside the DB reader) and the prior cache already held issues, the
    poller must re-fetch once; if that corroborating fetch is also
    empty, the cache is left untouched so the Overview doesn't blank.
    """
    cfg = _poll_cfg(tmp_path)
    pollers = OverviewPollers(cfg, pipeline_poll=0.01)
    rows = [
        {"repo": "jcronq/alice", "number": i, "state": "selected", "title": f"t{i}"}
        for i in range(1, 9)
    ]
    pollers._set("pipeline", {"issues": rows, "failed": []})

    calls = []

    def fake_fetch(repos):
        calls.append(repos)
        return [], []  # always empty — transient DB hiccup

    monkeypatch.setattr(P, "repos_sm_from_config", lambda: ["jcronq/alice"])
    monkeypatch.setattr(P, "fetch_issues", fake_fetch)
    note_calls = []
    monkeypatch.setattr(
        P,
        "ensure_review_notes",
        lambda rows, notes_dir: note_calls.append(list(rows)) or {"written": [], "deleted": []},
    )

    pollers._poll_pipeline_once()
    # Corroborating fetch was performed.
    assert len(calls) == 2, "transient-empty guard should re-fetch once"
    # Prior cache kept intact.
    cached = pollers.get("pipeline")
    assert cached is not None
    assert cached["issues"] == rows, "transient empty wiped the prior cache (#858)"
    # Review-note sync operated on the prior rows, not on empty.
    assert note_calls == [rows]


def test_poll_pipeline_once_transient_empty_recovers_on_recheck(
    tmp_path, monkeypatch
):
    """If the first fetch is a false-empty but the re-fetch succeeds,
    the cache advances to the new rows."""
    cfg = _poll_cfg(tmp_path)
    pollers = OverviewPollers(cfg, pipeline_poll=0.01)
    prior = [{"repo": "jcronq/alice", "number": 1, "state": "selected", "title": "t"}]
    pollers._set("pipeline", {"issues": prior, "failed": []})

    fresh = [
        {"repo": "jcronq/alice", "number": 1, "state": "building", "title": "t"},
        {"repo": "jcronq/alice", "number": 2, "state": "selected", "title": "u"},
    ]
    calls = []

    def fake_fetch(repos):
        calls.append(repos)
        return ([], []) if len(calls) == 1 else (fresh, [])

    monkeypatch.setattr(P, "repos_sm_from_config", lambda: ["jcronq/alice"])
    monkeypatch.setattr(P, "fetch_issues", fake_fetch)
    monkeypatch.setattr(P, "ensure_review_notes", lambda *a, **kw: {"written": [], "deleted": []})

    pollers._poll_pipeline_once()
    assert len(calls) == 2
    cached = pollers.get("pipeline")
    assert cached["issues"] == fresh


def test_poll_pipeline_once_fetch_exception_leaves_prior_cache(
    tmp_path, monkeypatch
):
    """``fetch_issues`` raising a real exception also preserves the
    prior cache — the outer ``except`` branch must not update."""
    cfg = _poll_cfg(tmp_path)
    pollers = OverviewPollers(cfg, pipeline_poll=0.01)
    prior = [{"repo": "jcronq/alice", "number": 1, "state": "selected", "title": "t"}]
    pollers._set("pipeline", {"issues": prior, "failed": []})

    def boom(repos):
        raise RuntimeError("gh timed out")

    monkeypatch.setattr(P, "repos_sm_from_config", lambda: ["jcronq/alice"])
    monkeypatch.setattr(P, "fetch_issues", boom)

    pollers._poll_pipeline_once()
    cached = pollers.get("pipeline")
    assert cached == {"issues": prior, "failed": []}


# ─── liveness cross-check (slice 2b re-scoped) ──────────────────────
#
# Jason's core bug: the "In flight" tier reads SM labels, but labels
# are a persistent-flag layer that lies about reality (worker dies →
# ``sm:building`` sticks around → tier shows a ghost). The checks
# below pin the behaviour of :func:`is_task_live` and
# :func:`partition_by_state_bucket`'s liveness kwarg.


def _scan_with_ps(lines, *, worktrees=(), spawn=frozenset()):
    """Shortcut for building a :class:`LivenessScan` from a list of
    cmdline strings."""
    return P_live.LivenessScan(
        ps_output="\n".join(lines),
        worktrees=tuple(worktrees),
        active_spawn_issues=spawn,
    )


def test_is_task_live_detects_matching_worktree_branch():
    from status_site.sources.pipeline_liveness import Worktree

    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(Worktree(path="/state/worker/alice-858-wt",
                             branch="feat/858-foo"),),
        active_spawn_issues=frozenset(),
    )
    live, ev = P_live.is_task_live({"number": 858}, scan)
    assert live and "worktree" in ev and "feat/858-foo" in ev


def test_is_task_live_detects_matching_worktree_path_when_branch_has_no_issue():
    from status_site.sources.pipeline_liveness import Worktree

    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(Worktree(path="/state/worker/alice-620-wt",
                             branch="fix/some-other-name"),),
        active_spawn_issues=frozenset(),
    )
    live, ev = P_live.is_task_live({"number": 620}, scan)
    assert live and "/alice-620-wt" in ev


def test_is_task_live_detects_matching_worker_process():
    """A ``claude`` / ``python ... worker`` line whose argv mentions
    the issue number is live evidence."""
    scan = _scan_with_ps(
        [
            "alice 12345 0.1 0.2 /usr/bin/claude --issue 858 --continue",
            "root  99999 0.0 0.0 /usr/sbin/sshd",  # noise — not matched
        ]
    )
    live, ev = P_live.is_task_live({"number": 858}, scan)
    assert live and ev.startswith("process ")


def test_is_task_live_detects_matching_active_spawn():
    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(),
        active_spawn_issues=frozenset({"858"}),
    )
    live, ev = P_live.is_task_live({"number": 858}, scan)
    assert live and "spawn-858" in ev


def test_is_task_live_rejects_row_with_no_signal_anywhere():
    scan = _scan_with_ps([])
    live, ev = P_live.is_task_live({"number": 858}, scan)
    assert not live and ev == "no live signal found"


def test_is_task_live_ignores_bash_and_random_lines_with_issue_number():
    """A random ``bash`` / editor line that happens to contain ``858``
    must not falsely pass liveness — the probe is pinned to worker
    runtimes (:data:`_WORKER_LINE_RE`)."""
    scan = _scan_with_ps(
        ["alice 12345 0.1 0.2 /bin/bash -c 'echo 858 pending'"]
    )
    live, _ = P_live.is_task_live({"number": 858}, scan)
    assert not live


def test_is_task_live_number_boundary_rejects_substring_match():
    """``858`` must not match inside ``1858`` / ``8580`` — bounded by
    non-digits."""
    scan = _scan_with_ps(["alice 11 0.0 0.0 /usr/bin/claude --issue 1858"])
    live, _ = P_live.is_task_live({"number": 858}, scan)
    assert not live


def test_is_task_live_handles_missing_number():
    """A row missing ``number`` can't be probed; return the explicit
    reason rather than silently passing or crashing."""
    live, ev = P_live.is_task_live({"state": "building"}, _scan_with_ps([]))
    assert not live and "no issue number" in ev


def test_empty_scan_marks_every_row_not_live():
    scan = P_live.LivenessScan.empty()
    for n in (10, 858, 999):
        live, _ = P_live.is_task_live({"number": n}, scan)
        assert not live


def test_partition_by_state_bucket_without_scan_is_unchanged():
    """Backward compat: calling without ``liveness_scan`` must behave
    exactly like the pre-slice-2b label-only partition."""
    rows = [
        {"state": "building", "number": 1},
        {"state": "reviewing", "number": 2},
        {"state": "blocked", "number": 3},
    ]
    buckets = P.partition_by_state_bucket(rows)
    assert [r["number"] for r in buckets["in_flight"]] == [1, 2]
    assert [r["number"] for r in buckets["blocked"]] == [3]


def test_partition_by_state_bucket_with_scan_mixes_live_and_stale():
    """Integration: 5 SM rows — 2 with a live signal, 3 without —
    yields 2 in_flight + 3 blocked, each stale row carrying a
    ``blocked_reason`` string. This is the whole point of slice 2b."""
    from status_site.sources.pipeline_liveness import Worktree

    rows = [
        # Live via worktree branch.
        {"state": "building", "number": 858, "repo": "jcronq/alice",
         "title": "wired"},
        # Live via ps line.
        {"state": "reviewing", "number": 620, "repo": "jcronq/alice",
         "title": "wired2"},
        # Three stale — no signal anywhere.
        {"state": "building", "number": 701, "repo": "jcronq/alice"},
        {"state": "building", "number": 702, "repo": "jcronq/alice"},
        {"state": "reviewing", "number": 703, "repo": "jcronq/alice"},
    ]
    scan = P_live.LivenessScan(
        ps_output="alice 123 0.0 0.0 /usr/bin/claude --issue 620 --continue",
        worktrees=(
            Worktree(path="/state/worker/alice-858-wt", branch="feat/858-foo"),
        ),
        active_spawn_issues=frozenset(),
    )
    buckets = P.partition_by_state_bucket(rows, liveness_scan=scan)
    assert [r["number"] for r in buckets["in_flight"]] == [858, 620]
    assert [r["number"] for r in buckets["blocked"]] == [701, 702, 703]
    for demoted in buckets["blocked"]:
        assert demoted["blocked_reason"].startswith("worker vanished — ")
        assert demoted["blocked_reason"].endswith("no live signal found")
    # Live rows pass through untouched — no ``blocked_reason`` smuggled in.
    for r in buckets["in_flight"]:
        assert "blocked_reason" not in r


def test_partition_by_state_bucket_with_scan_does_not_mutate_input():
    """The caller's row dicts must be left untouched — the scan
    shallow-copies demoted rows before stamping ``blocked_reason``."""
    from status_site.sources.pipeline_liveness import Worktree

    row = {"state": "building", "number": 999}
    original = dict(row)
    # Need an *observable* scan (has at least one worktree) to trigger
    # the demotion path — an empty scan now falls back to label-only
    # partition for sandbox safety, see test_partition_with_
    # unobservable_scan_falls_back_to_label_only.
    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(Worktree(path="/wt", branch="feat/42-unrelated"),),
        active_spawn_issues=frozenset(),
    )
    buckets = P.partition_by_state_bucket([row], liveness_scan=scan)
    assert row == original  # input dict is unchanged
    assert buckets["blocked"][0] is not row  # demoted copy, not same object
    assert buckets["blocked"][0]["blocked_reason"].startswith("worker vanished")


def test_partition_by_state_bucket_with_scan_leaves_already_blocked_rows_alone():
    """``sm:blocked`` rows don't get re-probed; they're already blocked."""
    rows = [{"state": "blocked", "number": 500}]
    buckets = P.partition_by_state_bucket(
        rows, liveness_scan=P_live.LivenessScan.empty()
    )
    assert buckets["blocked"] == rows
    assert "blocked_reason" not in buckets["blocked"][0]


def test_collect_active_spawn_issues_scans_spawn_dirs(tmp_path):
    """The dispatcher's spawn roots: ``spawn-<N>-<ts>`` outside
    ``.finished/`` are active; completed spawns move under
    ``.finished/``."""
    root_a = tmp_path / "dispatcher-spawns"
    root_a.mkdir()
    (root_a / "spawn-858-1794").mkdir()
    (root_a / "spawn-620-1795").mkdir()
    (root_a / ".finished").mkdir()
    (root_a / ".finished" / "spawn-100-1700").mkdir()  # must NOT be counted

    root_b = tmp_path / "speaking-spawns"
    root_b.mkdir()
    (root_b / "spawn-7-1796").mkdir()
    (root_b / "not-a-spawn").mkdir()  # name doesn't match pattern — skipped

    missing = tmp_path / "does-not-exist"  # OS error-safe

    out = P_live._collect_active_spawn_issues([root_a, root_b, missing])
    assert out == frozenset({"858", "620", "7"})


def test_livenessscan_collect_degrades_to_empty_when_paths_missing(tmp_path, monkeypatch):
    """Missing repo / spawn roots must produce an empty scan rather
    than crashing the renderer."""
    monkeypatch.setenv("GIT_REPO_DIR", str(tmp_path / "nope-repo"))
    scan = P_live.LivenessScan.collect(
        spawn_roots=(tmp_path / "nope-spawns",),
    )
    assert scan.worktrees == ()
    assert scan.active_spawn_issues == frozenset()
    # ps_output may be real (we ran ps on the host); that's fine — the
    # probe stays safe because a random ps line won't match an issue
    # number we didn't ask about.


def test_render_in_flight_demotes_rows_with_no_live_signal(tmp_path, monkeypatch):
    """End-to-end through the renderer: an issue tagged ``sm:building``
    whose worker has vanished must NOT appear in the In-flight tier
    and MUST appear in the Blocked tier with a ``blocked_reason``.

    Reproduces the exact ghost-building-task failure Jason flagged:
    SM state says "building" but there's no worker alive anywhere.
    """
    cfg = _mk_cfg_with_pipeline_states(
        tmp_path, [], monkeypatch
    )
    # Hand-build issues bypassing _mk_cfg_with_pipeline_states' auto-live patch.
    issues = [
        _issue(858, "sm:building", "art:code"),  # will be marked live
        _issue(999, "sm:building", "art:code"),  # stale (no signal)
    ]
    rows, failed = P.fetch_issues(
        ["jcronq/alice"], runner=_runner({"jcronq/alice": issues})
    )
    cfg.pollers._set("pipeline", {"issues": rows, "failed": failed})

    # Only #858 has a live signal.
    from status_site.sources.pipeline_liveness import Worktree
    fake_scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(Worktree(path="/tmp/alice-858-wt", branch="feat/858-x"),),
        active_spawn_issues=frozenset(),
    )
    monkeypatch.setattr(
        P_live.LivenessScan, "collect",
        classmethod(lambda cls, **kw: fake_scan),
    )

    in_flight = section_renderers(cfg)["in_flight"]()
    blocked = section_renderers(cfg)["blocked"]()

    inflight_nums = {r["number"] for r in in_flight["pipeline"]}
    blocked_nums = {r["number"] for r in blocked["rows"]}
    assert 858 in inflight_nums and 999 not in inflight_nums
    assert 999 in blocked_nums
    # The demoted row carries its reason for the UI.
    ghost = next(r for r in blocked["rows"] if r["number"] == 999)
    assert ghost["blocked_reason"].startswith("worker vanished — ")


# ─── sandbox-safety: unobservable scan is not a kill switch ─────────
#
# The alice-status container currently has no bind mounts for
# ``/state/worker`` or the host repo, so ``LivenessScan.collect()``
# produces an empty scan. Without this guard, every in-flight row
# demotes to blocked purely because the renderer can't see — the
# opposite of the slice 2b intent. See
# :meth:`LivenessScan.is_observable`.


def test_livenessscan_is_observable_true_when_worktree_present():
    from status_site.sources.pipeline_liveness import Worktree

    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(Worktree(path="/wt", branch="feat/1-x"),),
        active_spawn_issues=frozenset(),
    )
    assert scan.is_observable


def test_livenessscan_is_observable_true_when_spawn_issue_present():
    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(),
        active_spawn_issues=frozenset({"858"}),
    )
    assert scan.is_observable


def test_livenessscan_is_observable_false_on_empty_scan():
    """An empty scan is uninformative — probably means the renderer
    can't see worker signals (sandboxed container with no bind
    mounts), not that every worker is dead."""
    assert not P_live.LivenessScan.empty().is_observable


def test_livenessscan_is_observable_false_when_only_ps_output_present():
    """Non-empty ``ps`` alone is not evidence: inside the alice-status
    container ``ps aux`` lists container-local processes only, which
    won't include worker processes from the sibling ``alice``
    container. So require a worktree or spawn-dir signal too."""
    scan = P_live.LivenessScan(
        ps_output="some 1 0.0 0.0 /sbin/init\nalice 2 0.1 0.2 python -m status_site",
        worktrees=(),
        active_spawn_issues=frozenset(),
    )
    assert not scan.is_observable


def test_partition_with_unobservable_scan_falls_back_to_label_only():
    """When the scan has nothing to see, every ``sm:building`` /
    ``sm:reviewing`` row stays in ``in_flight`` — slice 2b degrades
    to the pre-slice behaviour rather than wiping the tier."""
    rows = [
        {"state": "building", "number": 1},
        {"state": "reviewing", "number": 2},
        {"state": "blocked", "number": 3},
    ]
    buckets = P.partition_by_state_bucket(
        rows, liveness_scan=P_live.LivenessScan.empty()
    )
    # Both in-flight rows pass through — no demotion happened.
    assert [r["number"] for r in buckets["in_flight"]] == [1, 2]
    assert [r["number"] for r in buckets["blocked"]] == [3]
    for r in buckets["in_flight"]:
        assert "blocked_reason" not in r
    # The sm:blocked row is untouched (never carried a reason).
    assert "blocked_reason" not in buckets["blocked"][0]


def test_partition_with_observable_scan_still_demotes_ghosts():
    """Regression pin: once the scan has ANY signal, slice 2b kicks
    back in and demotes rows with no matching evidence. Otherwise
    the sandbox-safety fallback would silently keep ghosts alive
    forever once a single worktree exists."""
    from status_site.sources.pipeline_liveness import Worktree

    rows = [
        {"state": "building", "number": 10},  # no signal → demote
        {"state": "building", "number": 20},  # worktree match → in_flight
    ]
    scan = P_live.LivenessScan(
        ps_output="",
        worktrees=(Worktree(path="/wt", branch="feat/20-x"),),
        active_spawn_issues=frozenset(),
    )
    buckets = P.partition_by_state_bucket(rows, liveness_scan=scan)
    assert [r["number"] for r in buckets["in_flight"]] == [20]
    assert [r["number"] for r in buckets["blocked"]] == [10]
    assert buckets["blocked"][0]["blocked_reason"].startswith("worker vanished")
