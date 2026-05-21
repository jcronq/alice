"""v3 handler for ``sm:building`` issues.

Mirrors v1's ``_process_building``: when a linked OPEN PR appears,
transition to ``sm:reviewing``. Otherwise the worker is still
producing the PR — no action; the dispatcher's TTL handles
escalation if the worker silently dies.

Also supports the universal ``[SM] continue`` self-loop so the
worker can report progress mid-build without transitioning.
"""

from __future__ import annotations

from typing import Any

from alice_forge.sm.comments import (
    Continue as ContinueParsed,
    ParseError,
    parse_comment,
)
from alice_forge.sm.result import (
    Continue,
    EmitParseError,
    HandlerResult,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def handle(issue: dict[str, Any], services: HandlerServices) -> HandlerResult | None:
    """Process one ``sm:building`` issue for one cadence.

    Decision order:

      1. If a linked PR exists and is OPEN, transition to
         ``sm:reviewing`` (EventTransition).
      2. Otherwise scan comments for ``[SM] continue`` / parse errors.
      3. No actionable input → ``None``.

    The PR check happens first because it's the dominant signal for
    this state — once the worker opens a draft PR, the issue should
    advance immediately on the next cadence regardless of what
    comments are in the thread.
    """
    number = issue["number"]

    # Step 1: linked PR check (event-driven transition).
    try:
        pr = services.find_linked_pr(services.repo, number)
    except Exception as exc:
        services.log(
            f"[sm-v3] building #{number}: failed to look up linked PR: {exc}"
        )
        pr = None

    if pr is not None:
        pr_state = (pr.get("state") or "").upper()
        if pr_state == "OPEN":
            pr_url = pr.get("url") or "<unknown>"
            return Transition(
                target=SMState.REVIEWING,
                reason=f"PR opened: {pr_url}",
                metadata={"pr_url": pr_url},
            )
        # PR exists but is CLOSED / MERGED — the build pipeline left
        # an artifact behind, but the state machine shouldn't react
        # to it from sm:building. v1 also no-ops here.

    # Step 2: scan for continue / parse errors.
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(
            f"[sm-v3] building #{number}: failed to list comments: {exc}"
        )
        return None

    for c in reversed(comments):
        body = c.get("body")
        if not isinstance(body, str):
            continue
        author = _comment_author(c)
        parsed = parse_comment(
            body, author, trusted_authors=services.trusted_authors
        )
        if parsed is None:
            continue
        if isinstance(parsed, ParseError):
            return EmitParseError(
                verb=parsed.reason.split()[1] if parsed.reason else "",
                reason=parsed.reason,
                reply_body=parsed.reply_body,
            )
        if isinstance(parsed, ContinueParsed):
            return Continue(reason=parsed.reason or "", findings=parsed.findings)

    return None


def _comment_author(c: dict[str, Any]) -> str | None:
    author = c.get("author")
    if isinstance(author, dict):
        login = author.get("login")
        return login if isinstance(login, str) else None
    if isinstance(author, str):
        return author
    user = c.get("user")
    if isinstance(user, dict):
        login = user.get("login")
        return login if isinstance(login, str) else None
    return None
