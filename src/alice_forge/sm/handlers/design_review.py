"""v3 handler for ``sm:design_review`` issues.

Mirrors v1's ``_process_design_review`` including the
DESIGN_REVISION_CAP guard (#164). Speaking owns this gate.

  * ``[SM] design-approved`` → ``sm:designed``. Clears revision counter.
  * ``[SM] design-revise reason=...`` → ``sm:designing``. Bumps the
    revision counter; if it would exceed the cap, transition to
    ``sm:rejected`` instead.
  * ``[SM] design-rejected reason=...`` → ``sm:rejected``.
  * ``[SM] continue`` → progress report.

The revision counter lives in the unified ledger as a
``design-revision`` record. Bumping increments the count metadata;
the cap is read from the metadata on each visit.
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


DESIGN_REVISION_CAP = 3
DESIGN_REVISION_NAME = "design-revision"


def handle(issue: dict[str, Any], services: HandlerServices) -> HandlerResult | None:
    number = issue["number"]
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] design_review #{number}: list_comments failed: {exc}")
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
        if isinstance(parsed, ParsedVerb):
            if parsed.verb is Verbs.DESIGN_APPROVED:
                return Transition(
                    target=SMState.DESIGNED,
                    reason="design-approved",
                    metadata={"clear_revision_counter": True},
                )
            if parsed.verb is Verbs.DESIGN_REJECTED:
                return Transition(
                    target=SMState.REJECTED,
                    reason=f"design-rejected: {parsed.reason or ''}",
                )
            if parsed.verb is Verbs.DESIGN_REVISE:
                # Bump the revision counter via the ledger. If the
                # post-bump count exceeds the cap, cut to rejected
                # with a design-revisions-capped audit reason.
                count = _current_revision_count(services, number) + 1
                if count > DESIGN_REVISION_CAP:
                    return Transition(
                        target=SMState.REJECTED,
                        reason=(
                            f"design-revisions-capped: hit cap "
                            f"({DESIGN_REVISION_CAP}) without approval"
                        ),
                        metadata={"revision_count": count},
                    )
                return Transition(
                    target=SMState.DESIGNING,
                    reason=f"design-revise: {parsed.reason or ''} (rev {count})",
                    metadata={
                        "revision_count": count,
                        "bump_revision_counter": True,
                    },
                )

    return None


def _current_revision_count(services: HandlerServices, number: int) -> int:
    rec = services.ledger.find(number, DESIGN_REVISION_NAME)
    if rec is None or rec.cleared_at is not None:
        return 0
    count = rec.metadata.get("count")
    if not isinstance(count, int):
        return 0
    return count


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
