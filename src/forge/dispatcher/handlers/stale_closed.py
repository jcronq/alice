"""Phase 1.6 sweep handler — closed issues that still carry a non-terminal ``sm:*`` label get routed to the right terminal state.
"""

from __future__ import annotations

from forge.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_stale_closed(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """Phase 1.6 sweep: route a closed issue with a non-terminal ``sm:*``
    label to its correct terminal state.

    The issue is already closed — we never re-open and we never close
    further; only labels and the ``[SM] transition`` audit comment are
    written. Decision tree:

      * linked PR merged + master CI green → ``sm:done``
      * linked PR merged + master CI red   → ``sm:rejected``
        (the merge happened but broke master; the work shipped-but-bad
        and downstream tracking should treat it as rejected pending
        follow-up.)
      * linked PR closed-unmerged          → ``sm:rejected``
      * no linked PR at all                → ``sm:rejected``
        (manual close or supersession — there's no merge artifact, so
        the safe terminal state is rejected.)

    A pending master CI verdict is treated as "wait" — we stay at the
    stale label and let the next pass re-evaluate. This keeps the
    sweep idempotent under flaky CI: we'd rather leave a stale label
    one more cadence than commit to ``sm:done`` before the build is
    actually green.
    """
    number = issue["number"]
    stale_label = _current_sm_label(issue)
    if stale_label is None:
        # Defensive: the helper already filters to non-terminal sm:*,
        # but if some odd label set sneaks through (multi-sm, typo),
        # don't guess.
        names = _label_names(issue)
        sm_labels_seen = [n for n in names if n.startswith("sm:")]
        log(
            f"[sm-dispatcher] sweep skip #{number}: "
            f"ambiguous sm:* label set {sm_labels_seen!r}"
        )
        return
    if stale_label in TERMINAL_SM_LABELS:
        # Belt-and-suspenders: helper's client-side filter should have
        # excluded this. If we got here anyway, do nothing.
        return

    # Resolve linked PR + outcome.
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] sweep: failed PR lookup for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    target_label: str
    reason: str
    if pr is None:
        # Closed with no PR linkage: manual close, supersession, or
        # a bot that closed without a "Closes #" reference. Without a
        # merge artifact the safe terminal is rejected.
        target_label = REJECTED_SM_LABEL
        reason = "issue closed without linked PR (manual close or supersession)"
    else:
        pr_number = pr.get("number")
        pr_state = (pr.get("state") or "").upper()
        if not isinstance(pr_number, int):
            log(
                f"[sm-dispatcher] sweep skip #{number}: "
                f"linked PR payload missing number ({pr!r})"
            )
            return
        if pr_state == "MERGED":
            try:
                merge_info = pr_merge_status(repo, pr_number)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] sweep: merge-status failed for "
                    f"PR #{pr_number}: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            sha = merge_info.get("merge_commit_oid")
            pr_url = merge_info.get("pr_url") or pr.get("url") or "<unknown>"
            if not sha:
                log(
                    f"[sm-dispatcher] sweep skip #{number}: "
                    f"PR #{pr_number} reports MERGED but no merge_commit_oid"
                )
                return
            try:
                ci = master_ci_status(repo, sha)
            except GHCommandError as exc:
                log(f"[sm-dispatcher] sweep: CI lookup failed for {sha[:8]}: {exc}")
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            conclusion = ci.get("conclusion")
            if conclusion is None or conclusion == "pending":
                # Hold the stale label one more cadence rather than
                # commit to a terminal before CI returns a verdict.
                log(
                    f"[sm-dispatcher] sweep wait #{number}: "
                    f"PR #{pr_number} merged but master CI is {conclusion!r}"
                )
                return
            if conclusion == "success":
                target_label = DONE_SM_LABEL
                reason = (
                    f"closed-by-merge sweep: PR #{pr_number} merged at {sha}, "
                    f"master CI success ({pr_url})"
                )
            else:
                # CI red post-merge: the work shipped but broke master.
                # Downgrade to rejected so a human picks up the follow-up;
                # we don't have the Phase 2 quality-gate plumbing yet.
                run_url = ci.get("run_url") or "<unknown>"
                target_label = REJECTED_SM_LABEL
                reason = (
                    f"closed-by-merge sweep: PR #{pr_number} merged at {sha} "
                    f"but master CI failure ({run_url})"
                )
        elif pr_state == "CLOSED":
            target_label = REJECTED_SM_LABEL
            reason = f"PR #{pr_number} closed without merge"
        else:
            # PR is still OPEN (or some state we don't recognise) and
            # the issue is closed. Possible scenarios: the PR was
            # un-merged after the fact, or the issue was hand-closed
            # while a PR still exists. Either way, don't sweep — let a
            # human (or a later phase) decide.
            log(
                f"[sm-dispatcher] sweep skip #{number}: "
                f"issue closed but linked PR #{pr_number} is {pr_state!r}"
            )
            return

    body = render_transition_comment(stale_label, target_label, reason)
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would sweep #{number}: "
            f"{stale_label} → {target_label} ({reason})"
        )
        report.swept += 1
        report.transitions.append((number, stale_label, target_label))
        return
    try:
        edit_labels(
            repo,
            number,
            add=[target_label],
            remove=[stale_label],
        )
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] sweep failed to transition #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.swept += 1
    report.transitions.append((number, stale_label, target_label))
    log(
        f"[sm-dispatcher] swept #{number}: "
        f"{stale_label} → {target_label} (issue stays closed)"
    )
