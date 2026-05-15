"""Handler for ``sm:design_review`` issues — speaking's review outcome (``design-approved`` / ``design-revise`` / ``design-rejected``) drives the transition, capped at :data:`DESIGN_REVISION_CAP` (issue #164).
"""

from __future__ import annotations

from alice_sm.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_design_review(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:design_review → sm:designed | sm:designing | sm:rejected.

    Speaking owns this gate. Two parseable verbs from a trusted author:

      * ``[SM] design-approved`` → ``sm:designed``. Clears the per-issue
        revision counter so a future re-entry starts fresh.
      * ``[SM] design-revise reason=... feedback=[[...]]`` → bumps
        :attr:`DispatcherState.design_revisions` for the issue. While
        the count is at or below :data:`DESIGN_REVISION_CAP` the issue
        bounces back to ``sm:designing`` for another iteration.
        On the (cap+1)th bounce the issue is routed to ``sm:rejected``
        with a ``[SM] design-revisions-capped`` audit so the operator
        sees why the loop terminated.

    Comments that aren't ``[SM] design-{approved,revise}`` are
    ignored; we wait for the next pass.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import DesignApproved, DesignRevise

    parsed = _find_parsed_comment_of_type(
        comments,
        (DesignApproved, DesignRevise),
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"awaiting design-approved / design-revise"
        )
        return

    if isinstance(parsed, DesignApproved):
        target = DESIGNED_SM_LABEL
        reason = "design-approved"
        transition_body = render_transition_comment(
            DESIGN_REVIEW_SM_LABEL, target, reason
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"design_review → designed (approved)"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, DESIGN_REVIEW_SM_LABEL, target)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[target],
                remove=[DESIGN_REVIEW_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to transition to designed: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_design_revisions(number)
        report.transitioned += 1
        report.transitions.append((number, DESIGN_REVIEW_SM_LABEL, target))
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"design_review → designed (approved)"
        )
        return

    # ----- design-revise branch -----
    # Use the pre-existing count to decide: if the count is already at
    # the cap, the new revise comment is the (cap+1)th bounce — reject.
    # Otherwise increment and bounce back to designing.
    prior = state.design_revision_count(number)
    if prior >= DESIGN_REVISION_CAP:
        capped_count = prior + 1
        reason = (
            f"design-revisions-capped count={capped_count} "
            f"cap={DESIGN_REVISION_CAP}"
        )
        transition_body = render_transition_comment(
            DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL, reason
        )
        audit_body = render_design_revisions_capped_comment(
            number, capped_count, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"design_review → rejected ({reason})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[REJECTED_SM_LABEL],
                remove=[DESIGN_REVIEW_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
            post_comment(repo, number, audit_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to transition to rejected: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_design_revisions(number)
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL)
        )
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"design_review → rejected ({reason})"
        )
        return

    # Under the cap → iterate.
    new_count = state.bump_design_revisions(number)
    reason = (
        f'design-revise iteration={new_count} '
        f'reason="{parsed.reason}" feedback=[[{parsed.feedback}]]'
    )
    transition_body = render_transition_comment(
        DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL, reason
    )
    if dry_run:
        # Roll back the bump so dry-run is side-effect-free on the
        # ledger; we already incremented above to render the reason.
        state.design_revisions[number] = new_count - 1
        if state.design_revisions[number] == 0:
            state.clear_design_revisions(number)
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"design_review → designing ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[DESIGNING_SM_LABEL],
            remove=[DESIGN_REVIEW_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        # Undo the ledger bump — the GH side didn't move, so the next
        # pass should observe the same revise comment and retry.
        state.design_revisions[number] = new_count - 1
        if state.design_revisions[number] == 0:
            state.clear_design_revisions(number)
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"failed to bounce to designing: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"design_review → designing ({reason})"
    )
