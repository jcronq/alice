"""Tests for the Ready-for-Review live renderer (issue #622).

These cover the section-surgery contract, age-string formatting, the
PR filter (``isDraft`` and ``reviewDecision``), and the section-body
render shape. All tests are hermetic — no real ``gh`` CLI calls; the
``gh()`` helper accepts an injectable subprocess runner.

Design:
``cortex-memory/designs/2026-11-05-issue622-ready-for-review-overview.md``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Sequence

import pytest

from alice_forge import review_queue_renderer as rqr


# ─── Helpers ──────────────────────────────────────────────────────────


def _fake_run(responses: dict[tuple[str, ...], tuple[int, str, str]]):
    """Build a subprocess runner that replays ``responses`` keyed on the
    `gh` subcommand tuple. Unknown commands raise ``KeyError`` so a
    missing stub is a loud test failure instead of a silent empty list.
    """

    def _runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
        assert cmd[0] == "gh", f"expected gh invocation, got {cmd!r}"
        key = tuple(cmd[1:])
        if key not in responses:
            raise KeyError(f"no fake response registered for {key!r}")
        rc, stdout, stderr = responses[key]
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=rc, stdout=stdout, stderr=stderr
        )

    return _runner


NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


# ─── replace_section ──────────────────────────────────────────────────


def test_replace_section_existing() -> None:
    """A section in the middle of the file is replaced; surrounding
    sections are untouched."""
    original = (
        "# Overview\n\n"
        "intro\n\n"
        "## Alpha\n\n"
        "alpha body\n\n"
        "## Ready for Review\n\n"
        "old body line 1\n"
        "old body line 2\n\n"
        "## Beta\n\n"
        "beta body\n"
    )
    new_body = "## Ready for Review\n\nfresh body\n"
    result = rqr.replace_section(original, "## Ready for Review", new_body)

    assert "## Alpha\n\nalpha body" in result
    assert "## Beta\n\nbeta body\n" in result
    assert "fresh body" in result
    assert "old body line 1" not in result
    assert "old body line 2" not in result
    # Headings still appear exactly once.
    assert result.count("## Ready for Review") == 1
    assert result.count("## Alpha") == 1
    assert result.count("## Beta") == 1


def test_replace_section_preserves_blank_line_before_next_heading() -> None:
    """After splicing, the markdown blank-line separator between the
    inserted section and the next ``## `` heading survives — otherwise
    the file renders with the two headings touching and loses its
    block structure."""
    original = (
        "## Ready for Review\n\n"
        "old body\n\n"
        "## Waiting on you\n\n"
        "tail\n"
    )
    # Emit a body shaped the way render_section does (single trailing \n).
    new_body = "## Ready for Review\n\nfresh body\n"
    result = rqr.replace_section(original, "## Ready for Review", new_body)
    assert "fresh body\n\n## Waiting on you" in result


def test_replace_section_at_eof() -> None:
    """A section at the end of the file (no trailing heading) is replaced
    through EOF."""
    original = (
        "# Overview\n\n"
        "intro\n\n"
        "## Ready for Review\n\n"
        "old item 1\n"
        "old item 2\n"
    )
    new_body = "## Ready for Review\n\nfresh\n"
    result = rqr.replace_section(original, "## Ready for Review", new_body)

    assert "old item 1" not in result
    assert "old item 2" not in result
    assert result.endswith("fresh\n")
    assert "intro" in result


def test_replace_section_missing_insert_before_waiting() -> None:
    """When ``## Ready for Review`` is absent but ``## Waiting on you``
    is present, the new section is inserted before ``Waiting on you``."""
    original = (
        "# Overview\n\n"
        "## Alpha\n\n"
        "alpha body\n\n"
        "## Waiting on you\n\n"
        "- [ ] task\n"
    )
    new_body = "## Ready for Review\n\nfresh\n"
    result = rqr.replace_section(original, "## Ready for Review", new_body)

    assert "## Ready for Review" in result
    assert "fresh" in result
    # The inserted section appears before Waiting on you.
    idx_review = result.index("## Ready for Review")
    idx_waiting = result.index("## Waiting on you")
    assert idx_review < idx_waiting
    # Alpha is still before the inserted section.
    assert result.index("## Alpha") < idx_review


def test_replace_section_missing_append() -> None:
    """When both headings are absent the new section is appended at EOF."""
    original = (
        "# Overview\n\n"
        "## Alpha\n\n"
        "alpha body\n"
    )
    new_body = "## Ready for Review\n\nfresh\n"
    result = rqr.replace_section(original, "## Ready for Review", new_body)

    assert result.startswith("# Overview")
    assert result.rstrip().endswith("fresh")
    assert "## Ready for Review" in result


# ─── age_str ──────────────────────────────────────────────────────────


def test_age_str_subhour() -> None:
    """``< 1h`` renders as the literal ``"<1h"``."""
    ts = (NOW - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    assert rqr.age_str(ts, now=NOW) == "<1h"


def test_age_str_hours() -> None:
    """Between 1h and 24h renders as ``"Xh"`` (floor)."""
    ts = (NOW - timedelta(hours=5, minutes=45)).isoformat().replace("+00:00", "Z")
    assert rqr.age_str(ts, now=NOW) == "5h"


def test_age_str_days() -> None:
    """>= 24h renders as ``"Xd"`` (floor)."""
    ts = (NOW - timedelta(days=3, hours=4)).isoformat().replace("+00:00", "Z")
    assert rqr.age_str(ts, now=NOW) == "3d"


# ─── render_section ───────────────────────────────────────────────────


def test_render_section_empty_all() -> None:
    """All three phases empty → every subsection shows ``*(none)*`` or
    the ``*(coming soon)*`` placeholder, and the italic blurb is present."""
    body = rqr.render_section([], [], [])
    assert body.startswith("## Ready for Review\n")
    assert "Items that passed through Alice's pipeline" in body
    assert "### Design review" in body
    assert "### PR review" in body
    assert "### Code review" in body
    # Design + PR → *(none)*; Code → *(coming soon)*.
    assert body.count("*(none)*") == 2
    assert "*(coming soon)*" in body


def test_render_section_pr_items() -> None:
    """A PR item renders with the ``repo#N`` link, title, age, and the
    distinct ``[Review →]`` link pointing at the PR URL."""
    pr = rqr.PRItem(
        repo="jcronq/modern-mud",
        number=373,
        title="bug-2 cesari cake box fix",
        url="https://github.com/jcronq/modern-mud/pull/373",
        age="2d",
    )
    body = rqr.render_section([], [pr], [])
    assert "### PR review" in body
    # Item line shape.
    expected = (
        "- [jcronq/modern-mud#373](https://github.com/jcronq/modern-mud/pull/373)"
        " — bug-2 cesari cake box fix — 2d — [Review →]"
        "(https://github.com/jcronq/modern-mud/pull/373)"
    )
    assert expected in body
    # Design phase still shows *(none)* and Code still shows *(coming soon)*.
    assert "*(none)*" in body
    assert "*(coming soon)*" in body


def test_render_section_design_items() -> None:
    """A design-review issue renders with the title and age; the Review →
    link falls back to the GitHub URL until #616 ships the Review tab UI."""
    design = rqr.DesignItem(
        repo="jcronq/alice",
        number=622,
        title="Live Ready for Review section",
        url="https://github.com/jcronq/alice/issues/622",
        age="4h",
    )
    body = rqr.render_section([design], [], [])
    expected = (
        "- [jcronq/alice#622](https://github.com/jcronq/alice/issues/622)"
        " — Live Ready for Review section — 4h — [Review →]"
        "(https://github.com/jcronq/alice/issues/622)"
    )
    assert expected in body


# ─── fetch_pr_review filters ──────────────────────────────────────────


def test_fetch_pr_review_filters_draft() -> None:
    """``isDraft: true`` PRs are dropped before they can hit the renderer."""
    draft_row = {
        "number": 1,
        "title": "wip",
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "createdAt": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "url": "https://github.com/jcronq/alice/pull/1",
    }
    ready_row = {
        "number": 2,
        "title": "ready for review",
        "isDraft": False,
        "reviewDecision": "REVIEW_REQUIRED",
        "createdAt": (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
        "url": "https://github.com/jcronq/alice/pull/2",
    }
    key = (
        "pr", "list", "--repo", "jcronq/alice",
        "--state", "open",
        "--json", "number,title,isDraft,reviewDecision,createdAt,url,author,statusCheckRollup",
    )
    runner = _fake_run({key: (0, json.dumps([draft_row, ready_row]), "")})
    items, failed = rqr.fetch_pr_review(["jcronq/alice"], runner=runner, now=NOW)

    assert failed == []
    assert [i.number for i in items] == [2]
    assert items[0].title == "ready for review"


def test_fetch_pr_review_filters_non_required() -> None:
    """PRs without ``reviewDecision == "REVIEW_REQUIRED"`` are dropped
    (e.g. ``""``, ``APPROVED``, ``CHANGES_REQUESTED``)."""
    rows = [
        {
            "number": 10, "title": "awaiting-reviewers",
            "isDraft": False, "reviewDecision": "",
            "createdAt": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "url": "https://github.com/jcronq/alice/pull/10",
        },
        {
            "number": 11, "title": "approved",
            "isDraft": False, "reviewDecision": "APPROVED",
            "createdAt": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "url": "https://github.com/jcronq/alice/pull/11",
        },
        {
            "number": 12, "title": "changes-requested",
            "isDraft": False, "reviewDecision": "CHANGES_REQUESTED",
            "createdAt": (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
            "url": "https://github.com/jcronq/alice/pull/12",
        },
        {
            "number": 13, "title": "ready",
            "isDraft": False, "reviewDecision": "REVIEW_REQUIRED",
            "createdAt": (NOW - timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
            "url": "https://github.com/jcronq/alice/pull/13",
        },
    ]
    key = (
        "pr", "list", "--repo", "jcronq/alice",
        "--state", "open",
        "--json", "number,title,isDraft,reviewDecision,createdAt,url,author,statusCheckRollup",
    )
    runner = _fake_run({key: (0, json.dumps(rows), "")})
    items, failed = rqr.fetch_pr_review(["jcronq/alice"], runner=runner, now=NOW)

    assert failed == []
    assert [i.number for i in items] == [13]


# ─── extra: failing fetch is reported, not raised ─────────────────────


def test_fetch_pr_review_failure_reports_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing ``gh`` query for one repo reports the repo in ``failed``
    and keeps processing the rest — individual failures shouldn't abort
    the whole phase."""

    def runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
        if "jcronq/alice" in cmd:
            return subprocess.CompletedProcess(
                args=list(cmd), returncode=1, stdout="", stderr="boom"
            )
        # modern-mud: return one ready PR
        return subprocess.CompletedProcess(
            args=list(cmd),
            returncode=0,
            stdout=json.dumps([
                {
                    "number": 373, "title": "cake box",
                    "isDraft": False, "reviewDecision": "REVIEW_REQUIRED",
                    "createdAt": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
                    "url": "https://github.com/jcronq/modern-mud/pull/373",
                }
            ]),
            stderr="",
        )

    items, failed = rqr.fetch_pr_review(
        ["jcronq/alice", "jcronq/modern-mud"], runner=runner, now=NOW
    )
    assert failed == ["jcronq/alice"]
    assert [i.number for i in items] == [373]
