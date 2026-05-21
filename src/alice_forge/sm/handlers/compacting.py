"""v3 handler for ``sm:compacting`` issues.

Mirrors v1's ``_process_compacting``:

  * On a trusted ``[SM] build-started`` comment, transition to
    ``sm:building`` — the agent has finished compaction and is now
    in BUILD mode.
  * On a trusted ``[SM] continue``, record the continue (the
    dispatcher handles I-4 hash dedup).
  * Otherwise: no parsed verb yet, the agent is still compacting.
    Return None and let the dispatcher's no-progress / TTL machinery
    handle escalation.

Legacy lane (the compact-signal path predates the SM v2 thinking +
speaking spawn architecture). Low traffic in production; the port
is mostly here for completeness of the state machine, not for
behavioral improvement.
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
    """Process one ``sm:compacting`` issue for one cadence.

    Returns:
      * :class:`Transition` to ``sm:building`` on a trusted
        ``[SM] build-started``.
      * :class:`Continue` on a trusted ``[SM] continue``.
      * :class:`EmitParseError` on a malformed ``[SM] `` comment.
      * ``None`` when no actionable input — the dispatcher's TTL +
        no-progress logic decides whether to escalate.
    """
    number = issue["number"]
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(
            f"[sm-v3] compacting #{number}: failed to list comments: {exc}"
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
        if isinstance(parsed, ParsedVerb):
            if parsed.verb is Verbs.BUILD_STARTED:
                return Transition(
                    target=SMState.BUILDING,
                    reason="build-started",
                )
            # Other parsed verbs aren't legal from sm:compacting.
            continue

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
