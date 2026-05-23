"""v3 handler for ``sm:designed`` issues.

Mirrors v1's ``_process_designed``: the issue is past design and
awaiting build. Two paths exist:

  1. ``(sm:designed, art:code)`` — spawn the speaking-agent build
     lane, then transition to ``sm:building``. The spawn dispatch
     is an :class:`EventTransition` (the dispatcher owns the spawn
     machinery; the handler only signals that the transition is
     expected).

  2. Legacy lane (anything not ``art:code``) — drop a
     ``compact.signal`` into the live thinking-agent spawn dir,
     transition to ``sm:compacting``.

Both paths are externally driven by the dispatcher's spawn logic.
In dry-run, v3 just inspects the labels and reports the *expected*
event-driven transition; the dispatcher applies the real spawn.

For the dual-run logger, the handler reports the expected next
transition as if the spawn succeeded. The diff job compares v3's
prediction against v1's actual.
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
)
from alice_forge.sm.services import HandlerServices


def handle(issue: dict[str, Any], services: HandlerServices) -> HandlerResult | None:
    number = issue["number"]

    # Parse comments for continue / parse errors first — same
    # newest-first pattern as the other handlers.
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] designed #{number}: list_comments failed: {exc}")
        comments = []

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

    # Event-driven transitions (``build-spawn-dispatch`` for art:code,
    # ``compact-signal-drop`` for the legacy compact lane) are owned by
    # v1's ``_process_designed``: it does the actual ``spawn_speaking``
    # / ``compact.signal`` write AND posts the resulting label change.
    # v3 used to return a *predicted* ``Transition(BUILDING)`` here for
    # the dual-run logger, but #333 made v3 authoritative — applying
    # the predicted transition flips the label without ever spawning a
    # build worker, leaving the issue at sm:building with no agent on
    # it (the actual bug observed on #294/#296/#297/#323 after #333
    # landed). Returning ``None`` for the event case keeps v3's
    # comment-driven decisions intact (parse-error / continue handled
    # above) and lets v1 do the spawn + label flip atomically.
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
