"""v3 handler for ``sm:reviewing`` issues — partial port.

v1's ``_process_reviewing`` is the dispatcher's largest handler
(~540 LOC). It covers four main flows:

  1. PR merged + master CI green + verify pass → ``sm:done`` (T2)
  2. PR merged + master CI red → ``sm:building`` rollback (T3)
  3. PR closed unmerged → ``sm:rejected``
  4. PR conflicting → 3-tier rebase / spawn / escalate dance

v3 partial port (this PR) covers (1), (2), (3) — the terminal
transitions driven by PR + CI state. The rebase machinery (4)
stays in v1 until Phase 3 because it requires the spawn-rebase
service which isn't in HandlerServices yet.

v3 also supports ``[SM] continue`` for the CI-pending case where
the reviewer wants to report progress while waiting on slow CI.
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
    number = issue["number"]

    # Step 1: PR + CI state inspection (the dominant signals).
    try:
        pr = services.find_linked_pr(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] reviewing #{number}: find_linked_pr failed: {exc}")
        pr = None

    if pr is not None:
        pr_state = (pr.get("state") or "").upper()

        # PR closed-unmerged → rejected
        if pr_state == "CLOSED":
            return Transition(
                target=SMState.REJECTED,
                reason="PR closed unmerged",
                metadata={"pr_state": pr_state, "transition_class": "closed-unmerged"},
            )

        # PR merged → T2 (success) or T3 (rollback) based on master CI.
        if pr_state == "MERGED":
            ci_verdict = _master_ci_verdict(services, issue)
            if ci_verdict == "green":
                return Transition(
                    target=SMState.DONE,
                    reason="T2: PR merged + master green + verify pass",
                    metadata={
                        "pr_state": pr_state,
                        "transition_class": "T2",
                    },
                )
            if ci_verdict == "red":
                return Transition(
                    target=SMState.BUILDING,
                    reason="T3: PR merged + master red — rollback",
                    metadata={
                        "pr_state": pr_state,
                        "transition_class": "T3",
                    },
                )
            # CI pending — fall through to comment scan for continue.

        # PR conflicting — v1's 3-tier rebase still owns this; v3
        # leaves the transition to v1 and returns None below.

    # Step 2: scan for continue / parse errors. CI-pending case
    # benefits from continue comments naming what the reviewer is
    # watching for.
    try:
        comments = services.list_comments(services.repo, number)
    except Exception as exc:
        services.log(f"[sm-v3] reviewing #{number}: list_comments failed: {exc}")
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

    # No transition condition met yet, no comment to act on. v1's
    # rebase / verify machinery may still act; v3 returns None.
    return None


def _master_ci_verdict(
    services: HandlerServices, issue: dict[str, Any]
) -> str | None:
    """Return ``"green"`` / ``"red"`` / ``None`` for the master CI status.

    The ``master_ci_status`` service may return a variety of shapes
    depending on how v1 wires it. We normalise to the three-way
    verdict; unknown values fall through to None (CI pending).
    """
    try:
        raw = services.master_ci_status(services.repo)
    except Exception as exc:
        services.log(
            f"[sm-v3] reviewing #{issue.get('number')}: "
            f"master_ci_status raised: {exc}"
        )
        return None

    # v1 commonly returns dict with conclusion key, or a string.
    if isinstance(raw, dict):
        conclusion = raw.get("conclusion") or raw.get("status")
        if isinstance(conclusion, str):
            return _normalise_verdict(conclusion)
    if isinstance(raw, str):
        return _normalise_verdict(raw)
    return None


def _normalise_verdict(s: str) -> str | None:
    """Map common GitHub CI strings to ``green`` / ``red`` / None."""
    s = s.upper()
    if s in {"SUCCESS", "GREEN", "PASS", "PASSED"}:
        return "green"
    if s in {"FAILURE", "RED", "FAIL", "FAILED", "ERROR", "CANCELLED"}:
        return "red"
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
