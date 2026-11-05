"""Tests for the in-process Ready-for-Review renderer.

The gh fetch + render logic is still in
``alice_forge.review_queue_renderer`` (unchanged from PR #623, kept
for one-shot CLI use). The new module ``status_site.sources.review_queue``
wraps it: strips the ``## Ready for Review`` heading (markers sit
under the heading in index.md) and appends the staleness footer.

#623's own 14 tests continue to run via ``tests/test_review_queue_renderer.py``
— this file adds splice-path assertions on top.
"""

from __future__ import annotations

from datetime import datetime, timezone


from alice_forge.review_queue_renderer import DesignItem, PRItem
from status_site.sources import review_queue as rq


def test_render_does_not_include_section_heading():
    out = rq.render([], [], agent_name="Alice") if False else rq.render([], [])
    assert "## Ready for Review" not in out


def test_render_keeps_three_subheadings_and_empty_states():
    out = rq.render([], [])
    assert "### Design review" in out
    assert "### PR review" in out
    assert "### Code review" in out
    assert out.count("*(none)*") >= 2  # design + pr empty; code is "(coming soon)"


def test_render_lists_design_items_with_url():
    design = [
        DesignItem(
            repo="jcronq/alice",
            number=638,
            title="Auto-render overview",
            url="https://github.com/jcronq/alice/issues/638",
            age="2h",
        )
    ]
    out = rq.render(design, [])
    assert "jcronq/alice#638" in out
    assert "Auto-render overview" in out
    assert "https://github.com/jcronq/alice/issues/638" in out


def test_render_lists_pr_items():
    prs = [
        PRItem(
            repo="jcronq/alice",
            number=631,
            title="Alice's queue live-render",
            url="https://github.com/jcronq/alice/pull/631",
            age="1d",
        )
    ]
    out = rq.render([], prs)
    assert "jcronq/alice#631" in out
    assert "1d" in out


def test_render_appends_staleness_footer():
    out = rq.render([], [], now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    footer = out.rstrip().splitlines()[-1]
    assert footer.startswith("*last_rendered:")
    assert "source: gh-review-queue" in footer


def test_fetch_passes_repos_from_config():
    """``fetch`` should read the same config key #622 introduced and
    pass the resulting repo lists down to the inner fetchers."""

    class FakeRunner:
        def __init__(self):
            self.cmds = []

        def __call__(self, cmd):
            self.cmds.append(list(cmd))
            import subprocess

            return subprocess.CompletedProcess(
                args=list(cmd), returncode=0, stdout="[]", stderr=""
            )

    runner = FakeRunner()

    def fake_cfg():
        return {
            "repos_pr": ["jcronq/alice", "jcronq/modern-mud"],
            "repos_design_review": ["jcronq/alice"],
        }

    rq.fetch(runner=runner, cfg_loader=fake_cfg)

    repos_design = [cmd[cmd.index("--repo") + 1]
                    for cmd in runner.cmds if cmd[1] == "issue"]
    repos_pr = [cmd[cmd.index("--repo") + 1]
                for cmd in runner.cmds if cmd[1] == "pr"]
    assert repos_design == ["jcronq/alice"]
    assert repos_pr == ["jcronq/alice", "jcronq/modern-mud"]
