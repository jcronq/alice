"""v3 handler for ``sm:designing`` issues.

Mirrors v1's ``_process_designing``: when the thinking agent posts
``[SM] design-ready note=[[<slug>]]``, transition to
``sm:design_review`` so Speaking's review gate fires. Otherwise the
agent is still drafting — no action, dispatcher TTL handles
escalation. Also supports the universal ``[SM] continue`` for
progress reports mid-design.
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
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] designing #{number}: list_comments failed: {exc}")
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
        if isinstance(parsed, ParsedVerb) and parsed.verb is Verbs.DESIGN_READY:
            return Transition(
                target=SMState.DESIGN_REVIEW,
                reason=f"design-ready note=[[{parsed.note}]]",
                metadata={"design_note": parsed.note},
            )

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
