"""Handler for ``sm:design_review`` issues — speaking's review outcome (``design-approved`` / ``design-revise`` / ``design-rejected``) drives the transition, capped at :data:`DESIGN_REVISION_CAP` (issue #164).
"""

from __future__ import annotations

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403
from alice_forge.sm.legacy.handlers.designed import (
    _find_design_ready_slug,
    _resolve_design_note_path,
)


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
    art_whitelist: frozenset[str],
    has_live_design_reviewer_spawn: Callable[[int], bool] | None,
    count_running_design_reviewer: Callable[[], int] | None,
    spawn_design_reviewer: Callable[..., str | None] | None,
    max_concurrent_design_reviewer_spawns: int,
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

    from alice_forge.comments import DesignApproved, DesignRevise

    parsed = _find_parsed_comment_of_type(
        comments,
        (DesignApproved, DesignRevise),
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        # Issue #344 — design-reviewer spawn dispatch. When no verdict
        # has been posted yet, kick off an automated reviewer instead of
        # waiting for a human (or alice's own active wake) to notice the
        # issue. The reviewer is a one-shot Opus call that emits one of
        # the two verdict prefixes; the dispatcher's next pass picks
        # that up via the parser above and runs the existing transition
        # logic.
        if (
            spawn_design_reviewer is None
            or has_live_design_reviewer_spawn is None
            or count_running_design_reviewer is None
        ):
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"awaiting design-approved / design-revise"
            )
            return
        if has_live_design_reviewer_spawn(number):
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"reviewer spawn in flight — waiting"
            )
            return
        if count_running_design_reviewer() >= max_concurrent_design_reviewer_spawns:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"reviewer pool full — deferring"
            )
            return
        # Resolve the design note from the design-ready comment slug.
        # Without a resolvable note the reviewer has nothing to evaluate;
        # fall back to the existing log-and-wait path so a human can
        # diagnose rather than spawning a doomed reviewer.
        slug = _find_design_ready_slug(comments, trusted_authors=trusted_authors)
        if slug is None:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"no design-ready slug found — cannot spawn reviewer"
            )
            return
        design_note_path = _resolve_design_note_path(slug)
        if design_note_path is None:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"design-ready slug {slug!r} did not resolve to a file"
            )
            return
        art_label = _current_art_label(issue, art_whitelist)
        if art_label is None:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"no whitelisted art:* label — cannot spawn reviewer"
            )
            return
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would spawn design-reviewer on "
                f"#{number} art={art_label} design_note={design_note_path}"
            )
            report.spawned += 1
            return
        try:
            spawn_id = spawn_design_reviewer(
                issue,
                art_label,
                repo,
                design_note_path=design_note_path,
            )
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to spawn design-reviewer: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        if spawn_id is None:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"design-reviewer spawn returned None"
            )
            return
        report.spawned += 1
        report.spawn_records.append((number, art_label, spawn_id))
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"spawned design-reviewer {spawn_id}"
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
