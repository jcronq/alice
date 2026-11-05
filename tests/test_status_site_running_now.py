"""Tests for the Running now source.

All inputs are fixtures — no live ``git worktree`` / ``ps`` / build-log
reads. The three design-called-out edge cases each get a test:

* "no PR yet" — worktree is alive, log tails but has no ``PR_URL:`` sentinel.
* "worktree listed but process dead" — ps output doesn't mention the path.
* "``PR_URL:`` sentinel present" — tailed log carries the sentinel.

Plus: branch → issue → task-record join, elapsed formatting, empty list,
and the staleness footer is always on the rendered output.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


from status_site.sources import running_now as rn


# ─── parse_worktrees ────────────────────────────────────────────────


def test_parse_worktrees_strips_refs_prefix():
    porcelain = (
        "worktree /home/alice/alice\n"
        "HEAD abc123\n"
        "branch refs/heads/master\n"
        "\n"
        "worktree /tmp/alice-build-638/alice\n"
        "HEAD def456\n"
        "branch refs/heads/feat/638-overview\n"
        "\n"
    )
    wts = rn.parse_worktrees(porcelain)
    assert len(wts) == 2
    assert wts[0].branch == "master"
    assert wts[1].branch == "feat/638-overview"
    assert wts[1].path == "/tmp/alice-build-638/alice"


def test_parse_worktrees_skips_bare_blocks_without_branch():
    porcelain = "worktree /x\nHEAD abc\nbare\n"
    assert rn.parse_worktrees(porcelain) == []


# ─── worktree_is_alive ──────────────────────────────────────────────


def test_worktree_alive_when_claude_line_mentions_path():
    ps = (
        "alice  101 0.1 0.2 ... /usr/bin/claude --repo /tmp/alice-build-638/alice\n"
        "alice  102 0.0 0.1 ... /usr/bin/python3 something-else\n"
    )
    assert rn.worktree_is_alive("/tmp/alice-build-638/alice", ps) is True


def test_worktree_dead_when_no_claude_process_mentions_it():
    ps = "alice 111 0.0 0.1 ... /usr/bin/python3 unrelated\n"
    assert rn.worktree_is_alive("/tmp/alice-build-638/alice", ps) is False


def test_worktree_dead_when_only_non_claude_matches():
    ps = "alice 111 0.0 0.1 ... /tmp/alice-build-638/alice/some-script\n"
    assert rn.worktree_is_alive("/tmp/alice-build-638/alice", ps) is False


# ─── tail_pr_url ────────────────────────────────────────────────────


def test_pr_url_sentinel_extracted_last_wins():
    log = (
        "starting...\n"
        "PR_URL: https://github.com/jcronq/alice/pull/777\n"
        "retrying with a different approach\n"
        "PR_URL: https://github.com/jcronq/alice/pull/780\n"
    )
    assert rn.tail_pr_url(log) == "https://github.com/jcronq/alice/pull/780"


def test_pr_url_missing_returns_empty_string():
    assert rn.tail_pr_url("some build output without the sentinel\n") == ""


# ─── collect_task_records ───────────────────────────────────────────


def test_collect_task_records_reads_frontmatter(tmp_path):
    (tmp_path / "issue-638.md").write_text(
        "---\nstatus: building\nissue: 638\n---\n\nbody here\n"
    )
    (tmp_path / "issue-620.md").write_text(
        "---\nstatus: blocked\nissue: '620'\n---\n\n"
    )
    out = rn.collect_task_records(tmp_path)
    assert set(out.keys()) == {"638", "620"}
    assert out["638"]["status"] == "building"
    assert out["620"]["status"] == "blocked"


def test_collect_task_records_falls_back_to_filename_number(tmp_path):
    (tmp_path / "issue-900.md").write_text("no frontmatter here\n")
    out = rn.collect_task_records(tmp_path)
    assert "900" in out


def test_collect_task_records_tolerates_missing_dir(tmp_path):
    missing = tmp_path / "nope"
    assert rn.collect_task_records(missing) == {}


# ─── build_running_tasks: the three edge cases ──────────────────────


def _wt(path, branch):
    return rn.Worktree(path=path, branch=branch)


def test_build_no_pr_yet_still_renders_line():
    wts = [_wt("/tmp/alice-build-638/alice", "feat/638-overview")]
    ps = "alice  1 0.0 0.0 claude --cwd /tmp/alice-build-638/alice\n"
    logs = [("starting, no sentinel yet\n", time.time() - 90)]
    rows = rn.build_running_tasks(wts, ps, logs, {}, now=time.time())
    assert len(rows) == 1
    r = rows[0]
    assert r.alive is True
    assert r.pr_url == ""
    assert r.elapsed  # non-empty


def test_build_worktree_listed_but_process_dead():
    wts = [_wt("/tmp/alice-build-620/alice", "fix/620-sonnet-dispatch")]
    ps = "alice 1 0.0 0.0 claude --cwd /some/other/path\n"
    rows = rn.build_running_tasks(wts, ps, [], {}, now=time.time())
    assert rows[0].alive is False


def test_build_pr_url_sentinel_is_surfaced():
    wts = [_wt("/tmp/alice-build-638/alice", "feat/638-overview")]
    ps = "alice 1 0.0 0.0 claude /tmp/alice-build-638/alice\n"
    log = (
        "...\nPR_URL: https://github.com/jcronq/alice/pull/777\n"
        "mentions /tmp/alice-build-638/alice to attribute\n"
    )
    rows = rn.build_running_tasks(wts, ps, [(log, time.time() - 120)], {}, now=time.time())
    assert rows[0].pr_url == "https://github.com/jcronq/alice/pull/777"


def test_build_joins_task_record_by_branch_issue_number():
    wts = [_wt("/tmp/alice-build-638/alice", "feat/638-overview")]
    task_records = {"638": {"status": "building", "issue": "638"}}
    rows = rn.build_running_tasks(wts, "", [], task_records, now=time.time())
    assert rows[0].issue == "638"
    assert rows[0].task_status == "building"


# ─── render ─────────────────────────────────────────────────────────


def test_render_empty_shows_nothing_state_and_footer():
    out = rn.render(
        [], now=datetime(2026, 11, 5, 18, tzinfo=timezone.utc).astimezone()
    )
    assert "nothing building" in out
    assert "last_rendered" in out
    assert "source: worktree-ps-build" in out


def test_render_lists_each_task():
    tasks = [
        rn.RunningTask(
            branch="feat/638-overview",
            worktree="/tmp/alice-build-638/alice",
            alive=True,
            pr_url="https://github.com/jcronq/alice/pull/777",
            elapsed="3m",
            task_status="building",
            issue="638",
        ),
        rn.RunningTask(
            branch="fix/620-dispatch",
            worktree="/tmp/alice-build-620/alice",
            alive=False,
            pr_url="",
            elapsed="2h",
            task_status="blocked",
            issue="620",
        ),
    ]
    out = rn.render(
        tasks, now=datetime(2026, 11, 5, 18, tzinfo=timezone.utc).astimezone()
    )
    assert "feat/638-overview" in out
    assert "alive" in out
    assert "https://github.com/jcronq/alice/pull/777" in out
    assert "fix/620-dispatch" in out
    assert "dead" in out
    assert "no PR yet" in out
    assert out.rstrip().splitlines()[-1].startswith("*last_rendered:")


def test_render_footer_format_matches_frontend_parser():
    """The frontend regex is ``/^last_rendered:\\s*(\\S+)/`` — the ISO
    timestamp must be the first whitespace-delimited token after the
    label so ``Date.parse`` can read it."""
    out = rn.render([])
    footer = out.rstrip().splitlines()[-1]
    assert footer.startswith("*last_rendered: ")
    # Drop the leading ``*`` so the Date.parse-side regex matches.
    inner = footer.strip("*").strip()
    assert inner.startswith("last_rendered:")
