"""Handler for ``sm:compacting`` issues — watches for the worker's ``[SM] build-started`` comment that signals compaction finished and the build can run.
"""

from __future__ import annotations

from forge.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_compacting(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    has_live_spawn: Callable[[int], bool] | None,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:compacting → sm:building on the agent's ``[SM] build-started`` comment.

    The thinking-agent is mid-compaction (container restart in
    progress). When it comes back up in BUILD mode it posts
    ``[SM] build-started`` — that's the dispatcher's signal to flip
    the label so :func:`_process_building` takes over and watches for
    the PR.

    The ``has_live_spawn`` callable is consulted as a confidence
    check: if the agent died during compaction (no live spawn) we
    still honor the build-started signal but log a warning, since the
    audit trail says the agent claimed it started; humans can sort it
    out from there.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from forge.comments import BuildStarted

    parsed = _find_parsed_comment_of_type(
        comments,
        BuildStarted,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"awaiting [SM] build-started"
        )
        return

    if has_live_spawn is not None and not has_live_spawn(number):
        log(
            f"[sm-dispatcher] compacting #{number}: WARNING — "
            f"build-started seen but no live spawn dir; agent may have "
            f"died during compaction. Transitioning anyway per audit trail."
        )

    reason = "build-started"
    transition_body = render_transition_comment(
        COMPACTING_SM_LABEL, BUILDING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"compacting → building (build-started)"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, COMPACTING_SM_LABEL, BUILDING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[BUILDING_SM_LABEL],
            remove=[COMPACTING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"failed to transition to building: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, COMPACTING_SM_LABEL, BUILDING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"compacting → building (build-started)"
    )
