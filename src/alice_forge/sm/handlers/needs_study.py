"""v3 handler for ``sm:needs_study`` issues.

Mirrors v1's ``_process_needs_study``:

  1. **Hint emission.** On first visit, emit a ``study-hint``
     side-effect (in v1 this writes ``inner/notes/sm-needs-study-
     issue<N>.md`` and posts ``[SM] study-hint-written``). The
     dispatcher applies the side-effect; the ledger dedupes.

  2. **Comment-driven transitions.** Newest-first scan:
       * ``[SM] study-complete art=<label> findings=[[<slug>]]`` →
         ``sm:selected`` (with optional art swap).
       * ``[SM] study-blocked reason=<...>`` → ``sm:blocked``.
       * ``[SM] study-rejected reason=<...>`` → ``sm:rejected``.
       * ``[SM] continue reason=<...>`` → record progress, no
         transition.

  3. **Vault auto-advance.** If no parsed verb yet AND the
     research resolver returns a slug for this issue, emit a
     ``vault-auto-advance`` side-effect that posts a synthetic
     ``[SM] study-complete art=art:research_note findings=[[<slug>]]
     auto-posted=true`` on thinking's behalf. The next cadence
     picks the synthesized comment up in step 2.
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


STUDY_HINT_NAME = "study-hint"
VAULT_AUTO_ADVANCE_NAME = "vault-auto-advance"


def handle(issue: dict[str, Any], services: HandlerServices) -> HandlerResult | None:
    """Process one ``sm:needs_study`` issue for one cadence."""
    number = issue["number"]

    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(
            f"[sm-v3] needs_study #{number}: failed to list comments: {exc}"
        )
        return None

    # Step 2 first: if there's already a parsed transition verb in
    # the thread, take it immediately. Hint emission is wasted if
    # the issue is about to leave the state anyway.
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
            if parsed.verb is Verbs.STUDY_COMPLETE:
                return Transition(
                    target=SMState.SELECTED,
                    reason=f"study-complete findings=[[{parsed.findings}]]",
                    art_swap=parsed.art_label,
                    metadata={"findings": parsed.findings},
                )
            if parsed.verb is Verbs.STUDY_BLOCKED:
                return Transition(
                    target=SMState.BLOCKED,
                    reason=f"study-blocked: {parsed.reason or ''}",
                )
            if parsed.verb is Verbs.STUDY_REJECTED:
                return Transition(
                    target=SMState.REJECTED,
                    reason=f"study-rejected: {parsed.reason or ''}",
                )

    # Step 3: vault auto-advance. If a research note in the vault
    # already resolves this issue, synthesize the study-complete
    # comment thinking forgot to post.
    if services.research_resolver is not None:
        try:
            slug = services.research_resolver(number)
        except Exception as exc:
            services.log(
                f"[sm-v3] needs_study #{number}: research resolver raised: {exc}"
            )
            slug = None
        if slug:
            return SideEffect(
                name=VAULT_AUTO_ADVANCE_NAME,
                body=(
                    f"[SM] study-complete art=art:research_note "
                    f"findings=[[{slug}]] auto-posted=true\n"
                ),
                ttl_seconds=None,
                metadata={"resolving_slug": slug},
            )

    # Step 1: hint emission (only if no transition + no auto-advance).
    if not services.ledger.is_emitted_active(
        number, STUDY_HINT_NAME, services.now()
    ):
        return SideEffect(
            name=STUDY_HINT_NAME,
            body=(
                f"[SM] study-hint-written path=inner/notes/sm-needs-study-issue{number}.md\n\n"
                f"Hint file written for thinking-agent to pick up on next wake.\n"
            ),
            ttl_seconds=None,
            metadata={"hint_path": f"inner/notes/sm-needs-study-issue{number}.md"},
        )

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
