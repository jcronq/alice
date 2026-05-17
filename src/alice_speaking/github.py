"""GitHub helpers for the speaking hemisphere.

The single canonical wrapper around ``gh issue create`` for issues
Speaking files autonomously. Always call this instead of shelling out
to ``gh issue create`` directly so the body picks up
:data:`SELF_FILED_MARKER` — the marker the watcher
(:mod:`alice_watchers.github`) keys off to suppress the redundant
``new_issue`` → thinking-analysis → attempt-issue-fix loop on Alice's
own tickets (issue #226).

The marker is duplicated as a constant in both modules deliberately:
``alice_speaking`` and ``alice_watchers`` don't import each other in
the runtime, so a shared constant would mean a new shared module and
extra import surface for one short string. The pair is covered by
unit tests on both sides — if one drifts, the watcher test that round-
trips a self-filed body through the watcher will catch it.
"""

from __future__ import annotations

import subprocess
from typing import Iterable


# Keep in sync with ``alice_watchers.github.SELF_FILED_MARKER``.
SELF_FILED_MARKER = "<!-- alice-self-filed -->"


def stamp_self_filed(body: str) -> str:
    """Append the self-filed marker to a body, idempotently.

    Returns the body unchanged when the marker is already present (the
    common case when a caller is composing the body from a template
    that already includes it). Otherwise the marker is appended on its
    own trailing line, separated from the prior content by a blank
    line so the rendered issue body keeps a clean visual break before
    the (invisible) HTML comment.
    """
    if SELF_FILED_MARKER in body:
        return body
    trimmed = body.rstrip()
    if not trimmed:
        return SELF_FILED_MARKER + "\n"
    return f"{trimmed}\n\n{SELF_FILED_MARKER}\n"


def create_issue(
    repo: str,
    title: str,
    body: str,
    *,
    labels: Iterable[str] = (),
    gh_bin: str = "gh",
    timeout: int = 60,
) -> str:
    """Shell out to ``gh issue create``, stamping the self-filed marker.

    Returns the new issue's URL (``gh`` prints it on stdout). Raises
    :class:`subprocess.CalledProcessError` on failure so the caller
    can surface the error rather than silently dropping the ticket.

    Use this for every autonomous issue Speaking files. Direct
    ``gh issue create`` calls bypass the watcher-suppression marker
    and re-introduce the issue #226 noise loop.
    """
    stamped = stamp_self_filed(body)
    args: list[str] = [
        gh_bin,
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        stamped,
    ]
    for label in labels:
        args.extend(["--label", label])
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


__all__ = ["SELF_FILED_MARKER", "create_issue", "stamp_self_filed"]
