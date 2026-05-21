"""v3 handler for ``sm:selected`` issues.

Partial port of v1's ``_process_selected``. The full v1 handler is
the most complex in the dispatcher — it covers return-to-study,
dependency gating (issue #176), the hello-comment, the T1
linked-PR transition, and the Phase 2 spawn dispatch (with
concurrency caps and dedup). The v3 dry-run handler covers the
comment-driven and event-driven decisions; spawn dispatch + dep
gating + hello stay in v1 until the full Phase 3 cutover.

What v3 covers here:

  * ``[SM] return-to-study reason=<...>`` → ``sm:needs_study``.
  * Linked PR open → ``sm:reviewing`` (T1 transition).
  * ``[SM] continue`` → progress.
  * Parse errors → loud reply.

What v3 does NOT cover yet (v1 keeps owning):

  * dependency check (rejected dep → sm:blocked)
  * hello-comment emission (dedup'd by ledger; will move to v3
    when the unified ledger replaces v1's hello_commented list)
  * Phase 2 spawn dispatch (event-driven, requires spawn services
    not in HandlerServices today)

The diff job will surface these as v1-actual ≠ v3-predicted; the
divergences are expected and acceptable until Phase 3.
"""

from __future__ import annotations

from typing import Any

from alice_forge.sm.comments import (
    Continue as ContinueParsed,
    ParseError,
    ParsedVerb,
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
from alice_forge.sm.transitions import Verbs


def handle(issue: dict[str, Any], services: HandlerServices) -> HandlerResult | None:
    number = issue["number"]

    # Step 1: linked PR check (T1 transition — dominant signal).
    try:
        pr = services.find_linked_pr(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] selected #{number}: find_linked_pr failed: {exc}")
        pr = None

    if pr is not None:
        pr_state = (pr.get("state") or "").upper()
        if pr_state == "OPEN":
            pr_url = pr.get("url") or "<unknown>"
            return Transition(
                target=SMState.REVIEWING,
                reason=f"T1: PR opened: {pr_url}",
                metadata={"pr_url": pr_url, "transition_class": "T1"},
            )

    # Step 2: scan for return-to-study / continue / parse errors.
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] selected #{number}: list_comments failed: {exc}")
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
        if isinstance(parsed, ParsedVerb) and parsed.verb is Verbs.RETURN_TO_STUDY:
            return Transition(
                target=SMState.NEEDS_STUDY,
                reason=f"return-to-study: {parsed.reason or ''}",
            )

    # Step 3: nothing actionable from v3's perspective. v1's spawn /
    # hello / dep-check still own this state until Phase 3.
    return None


def _comment_author(c: dict[str, Any]) -> str | None:
    author = c.get("author")
    if isinstance(author, dict):
        return author.get("login")
    if isinstance(author, str):
        return author
    user = c.get("user")
    if isinstance(user, dict):
        return user.get("login")
    return None
