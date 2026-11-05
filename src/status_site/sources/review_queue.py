"""Ready-for-Review live renderer — pure functions ported from
``alice_forge.review_queue_renderer`` (PR #623) so the status_site
server can splice the section in at request time, instead of a
standalone s6 timer rewriting ``index.md`` on disk.

The gh-query code and the render shape are unchanged from #623 —
only the delivery mechanism differs. Importing from this module is
the sanctioned public API; ``alice_forge.review_queue_renderer``
stays available for one-shot CLI / dry-run use (``python -m
alice_forge.review_queue_renderer``) but is no longer wired to an
s6 service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from alice_forge.review_queue_renderer import (
    DEFAULT_REPOS_DESIGN_REVIEW,
    DEFAULT_REPOS_PR,
    DesignItem,
    PRItem,
    SubprocessRunner,
    _load_config,
    _coerce_repos,
    fetch_design_review_issues,
    fetch_pr_review,
    render_section,
)
from status_site.sources.common import staleness_footer

SOURCE_NAME = "gh-review-queue"


def fetch(
    *,
    runner: SubprocessRunner | None = None,
    now: datetime | None = None,
    cfg_loader=_load_config,
) -> tuple[list[DesignItem], list[PRItem], list[str], list[str]]:
    """Run both gh queries and return ``(design, pr, design_failed,
    pr_failed)``. Repos are loaded from
    ``alice.config.json.review_queue_renderer`` with the same keys
    #622 uses; ``runner`` is injectable for tests."""
    cfg = cfg_loader()
    repos_pr = _coerce_repos(cfg.get("repos_pr"), DEFAULT_REPOS_PR)
    repos_design = _coerce_repos(
        cfg.get("repos_design_review"), DEFAULT_REPOS_DESIGN_REVIEW
    )
    design_items, design_failed = fetch_design_review_issues(
        repos_design, runner=runner, now=now
    )
    pr_items, pr_failed = fetch_pr_review(repos_pr, runner=runner, now=now)
    return design_items, pr_items, design_failed, pr_failed


def render(
    design_items: Sequence[DesignItem],
    pr_items: Sequence[PRItem],
    *,
    design_failed: Iterable[str] = (),
    pr_failed: Iterable[str] = (),
    now: datetime | None = None,
) -> str:
    """Render the Ready-for-Review region body WITHOUT the ``## Ready
    for Review`` heading (the splice markers sit under the heading in
    ``index.md`` so the heading stays).

    Preserves #623's three-phase shape (Design / PR / Code review) and
    appends the standard staleness footer. Failed-repo lists are not
    currently surfaced in the rendered body — they'd just repeat the
    error that the background poll thread already logs. Kept in the
    signature so the server can decide to render a warning line if
    it wants to later."""
    _ = design_failed, pr_failed  # reserved
    full = render_section(design_items, pr_items)
    # ``render_section`` includes the ``## Ready for Review`` heading
    # as its first line. Strip it so the splice sits inside the
    # existing heading block.
    lines = full.splitlines()
    if lines and lines[0].startswith("## Ready for Review"):
        lines = lines[1:]
        # Drop the leading blank that followed the heading.
        if lines and not lines[0].strip():
            lines = lines[1:]
    body = "\n".join(lines).rstrip()
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)
