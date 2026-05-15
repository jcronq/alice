"""Handler for ``sm:draft`` issues — converts a trusted ``[SM] route-to-study`` comment into the ``sm:draft → sm:needs_study`` transition.
"""

from __future__ import annotations

from alice_sm.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_draft(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    art_whitelist: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:draft → sm:needs_study on a trusted ``[SM] route-to-study`` comment.

    The ``art=<art-label>`` field is optional. When present *and*
    different from the issue's current ``art:*`` label, the dispatcher
    swaps the label atomically with the state transition.
    """
    number = issue["number"]
    decision = evaluate_trust(issue, trusted_authors=trusted_authors)
    if not decision.accepted:
        log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
        report.skipped_trust += 1
        return

    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] draft #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import RouteToStudy

    parsed = _find_parsed_comment_of_type(
        comments,
        RouteToStudy,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        return

    add_labels = [NEEDS_STUDY_SM_LABEL]
    remove_labels = [DRAFT_SM_LABEL]
    reason = "route-to-study"
    if parsed.art_label is not None:
        current_art = _current_art_label(issue, art_whitelist)
        if parsed.art_label != current_art:
            add_labels.append(parsed.art_label)
            if current_art is not None:
                remove_labels.append(current_art)
        reason += f" art={parsed.art_label}"

    transition_body = render_transition_comment(
        DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"draft → needs_study ({reason})"
        )
        report.transitioned += 1
        report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
        return
    try:
        edit_labels(repo, number, add=add_labels, remove=remove_labels)
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] draft #{number}: "
            f"failed route-to-study transition: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"draft → needs_study ({reason})"
    )
