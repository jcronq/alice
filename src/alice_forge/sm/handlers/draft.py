"""v3 handler for ``sm:draft`` issues.

Replicates v1's ``_process_draft`` semantics:

  * On a trusted ``[SM] route-to-study`` comment, transition to
    ``sm:needs_study``. Optional ``art=<label>`` swaps the art label
    during the transition. Reserved for research-type artifacts that
    genuinely need a study phase.
  * On a trusted ``[SM] select`` comment, transition directly to
    ``sm:selected``, skipping ``sm:needs_study``. Optional
    ``art=<label>`` swaps the art label during the transition. The
    default path for non-research artifacts (code, config_change,
    experiment) where triage already knows the work.
  * On a trusted ``[SM] reject reason=<...>``, transition to
    ``sm:rejected``.
  * On a trusted ``[SM] continue reason=<...>`` self-loop, record
    the continue (the dispatcher handles I-4 hash dedup).
  * Otherwise (no parsed verb): if the dispatcher has not already
    emitted a ``triage-surface`` for this issue, emit one. The
    dedup is the unified ledger's ``triage-surface`` record.
  * Repeated cycles with no useful comment escalate via the
    standard three-strike no-progress path (the dispatcher
    enforces).

The handler is pure: it returns a :class:`HandlerResult` and lets
the dispatcher apply the side-effect. No direct GitHub calls.
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
    SideEffect,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState
from alice_forge.sm.transitions import Verbs


TRIAGE_SURFACE_NAME = "triage-surface"


def handle(issue: dict[str, Any], services: HandlerServices) -> HandlerResult | None:
    """Process one ``sm:draft`` issue for one cadence.

    Returns:
      * :class:`Transition` if a trusted route-to-study / reject parsed.
      * :class:`Continue` on a trusted continue.
      * :class:`SideEffect` (triage-surface) on first visit with no parsed verb.
      * :class:`EmitParseError` on a malformed ``[SM] `` comment.
      * ``None`` when the issue has no actionable input AND the
        triage-surface side-effect is already in flight — the
        dispatcher's no-progress detector handles the strike
        accounting from there.

    The handler does NOT enforce trust beyond what
    :func:`parse_comment` already checks; untrusted authors come
    back as ParseError.
    """
    number = issue["number"]
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(
            f"[sm-v3] draft #{number}: failed to list comments: {exc}"
        )
        return None  # Transient; let the dispatcher retry next cadence.

    # Scan newest-first; the first parsed verb from a trusted
    # author wins. Parse errors are surfaced as soon as found
    # (matches v1 audit behavior on malformed comments).
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
            if parsed.verb is Verbs.ROUTE_TO_STUDY:
                return Transition(
                    target=SMState.NEEDS_STUDY,
                    reason="route-to-study",
                    art_swap=parsed.art_label,
                )
            if parsed.verb is Verbs.SELECT:
                return Transition(
                    target=SMState.SELECTED,
                    reason="select",
                    art_swap=parsed.art_label,
                )
            if parsed.verb is Verbs.REJECT:
                return Transition(
                    target=SMState.REJECTED,
                    reason=parsed.reason or "rejected",
                )
            # Any other parsed verb is not legal from sm:draft; the
            # dispatcher will surface it as an unexpected-verb event
            # (separate from parse errors — the verb parsed fine, just
            # doesn't apply to this state).
            continue

    # No parsed verb. If we've never emitted a triage-surface for
    # this issue, do so now. Otherwise return None and let the
    # dispatcher's no-progress / TTL logic decide whether to
    # escalate.
    if services.ledger.is_emitted_active(
        number, TRIAGE_SURFACE_NAME, services.now()
    ):
        return None

    return SideEffect(
        name=TRIAGE_SURFACE_NAME,
        body=_render_triage_surface_body(issue),
        ttl_seconds=None,  # cleared when the issue transitions out of draft
        metadata={"issue_url": issue.get("url") or _issue_url(services.repo, number)},
    )


def _comment_author(c: dict[str, Any]) -> str | None:
    """Extract the comment author login, tolerating both v1 + gh shapes."""
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


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _render_triage_surface_body(issue: dict[str, Any]) -> str:
    """Render the triage-surface body for Speaking to consume.

    Compatible shape with v1's ``_render_triage_surface`` — Speaking
    already understands this format.
    """
    number = issue["number"]
    title = issue.get("title") or "(no title)"
    return (
        f"[SM] triage-surface number={number} title={title!r}\n\n"
        f"Issue is at sm:draft awaiting triage. Decide:\n"
        f"  1. [SM] select art=<label>?            (skip study — code/config/experiment)\n"
        f"  2. [SM] route-to-study art=<label>?    (study first — research)\n"
        f"  3. [SM] reject reason=<one-liner>\n"
        f"  4. [SM] continue reason=<triage progress>  (self-loop with new info)\n"
    )
