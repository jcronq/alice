"""Handler for ``sm:designing`` issues — promotes a ``[SM] design-ready`` comment from the thinking agent into the ``sm:designing → sm:design_review`` transition.
"""

from __future__ import annotations

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403


def _process_designing(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:designing → sm:design_review on a fresh ``[SM] design-ready`` comment.

    The thinking-agent is running and producing a design draft. When it
    emits ``[SM] design-ready note=[[...]]`` the dispatcher relabels the
    issue ``sm:design_review`` and posts a ``[SM] design-ready-audit``
    so Speaking's review loop knows to pick it up.

    No design-ready comment yet → no action; the agent is still
    working. The handler is otherwise idempotent: once the label flips
    to ``sm:design_review`` the issue's next pass goes through
    :func:`_process_design_review` instead.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_forge.comments import DesignReady

    parsed = _find_parsed_comment_of_type(
        comments,
        DesignReady,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"no [SM] design-ready comment yet"
        )
        return

    reason = f"design-ready note=[[{parsed.note}]]"
    transition_body = render_transition_comment(
        DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL, reason
    )
    audit_body = render_design_ready_audit_comment(
        number, parsed.note, timestamp=now_iso()
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"designing → design_review ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[DESIGN_REVIEW_SM_LABEL],
            remove=[DESIGNING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
        post_comment(repo, number, audit_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"failed to transition to design_review: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designing → design_review ({reason})"
    )
