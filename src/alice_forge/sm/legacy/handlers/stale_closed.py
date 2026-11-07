"""Phase 1.6 sweep handler — closed issues that still carry a non-terminal ``sm:*`` label get routed to the right terminal state.
"""

from __future__ import annotations

import os

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403


#: Env override for the ``sweeper.rejected_dry_run`` config key, scoped
#: to the Phase 1.6 REJECT path (issue #784). Lets an operator toggle
#: without redeploying ``alice.config.json``. When unset, the loaded
#: :class:`SweeperConfig` is authoritative; when set, the env wins.
#:
#: Semantics (reject-only): the REJECT branch (no-linked-PR + closed-
#: unmerged) is the only path gated by this flag. The ``sm:done`` branch
#: (merge is terminal, no ambiguity) is never gated — see the
#: "Dry-run audit mode" section of the #784 design note.
_SWEEPER_SAFE_MODE_ENV = "SM_SWEEPER_SAFE_MODE"


def _safe_mode_enabled(
    config: "SweeperConfig | None" = None,
) -> bool:
    """Return True when the Phase 1.6 REJECT path should dry-run even if
    the dispatcher's ``dry_run`` flag is False (issue #784).

    Env var :envvar:`SM_SWEEPER_SAFE_MODE` wins when set (any of
    ``0/false/no/off/empty`` disables, anything else enables). When
    unset, falls back to ``config.rejected_dry_run``; when that too is
    absent (``config is None``), defaults to True so a missing config
    section does not accidentally flip to live-write.

    Read lazily per call so an operator can toggle between cadences
    without restarting the dispatcher.
    """
    raw = os.environ.get(_SWEEPER_SAFE_MODE_ENV)
    if raw is not None:
        return raw.strip().lower() not in {"0", "false", "no", "off", ""}
    if config is None:
        return True
    return bool(config.rejected_dry_run)


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
    find_open_linked_prs: "Callable[[str, int], list[int]] | None" = None,
    sweeper_config: "SweeperConfig | None" = None,
) -> None:
    """Phase 1.6 sweep: route a closed issue with a non-terminal ``sm:*``
    label to its correct terminal state.

    The issue is already closed — we never re-open and we never close
    further; only labels and the ``[SM] transition`` audit comment are
    written. Decision tree (issue #784 — the merged-PR branch no
    longer depends on master CI color):

      * linked PR merged                   → ``sm:done``
        (work shipped; downstream CI color at merge time is a property
        of master, not of this PR's own diff. CI remediation runs on a
        separate loop — see watcher #782 — and doesn't belong in a
        per-issue close verdict.)
      * linked PR closed-unmerged, no open follow-up PR → ``sm:rejected``
      * linked PR closed-unmerged, open follow-up PR present → wait
        (another attempt is in flight; the sweeper leaves the stale
        label alone and re-evaluates on the next cadence.)
      * no linked PR at all                → ``sm:rejected``
        (manual close or supersession — there's no merge artifact, so
        the safe terminal state is rejected.)

    ``find_open_linked_prs`` (issue #784): optional callable returning
    the list of OPEN PR numbers that close this issue. When None (the
    legacy default), the "open follow-up" check is skipped and
    closed-unmerged always resolves to ``sm:rejected``.

    Safe-mode (issue #784): when :envvar:`SM_SWEEPER_SAFE_MODE` is
    truthy (default on), the handler forces a dry-run even if the
    dispatcher is acting. The intended rollout is ``safe_mode=on`` for
    the first week post-merge so operators can audit the label-change
    counters before enabling live writes.
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
            # Issue #784: a merged PR is always ``sm:done``, regardless
            # of master CI color. The CI verdict reflects the state of
            # master, not of this PR's own diff — pre-existing master
            # red cascades to every merged PR and was silently killing
            # completed work (see #613, #614, #616, #617, #618, #620,
            # #621, #656). CI remediation runs on a separate watcher
            # (#782) and doesn't belong in the close verdict.
            #
            # We still fetch CI status for logging/observability so the
            # done-line records what the old code would have done.
            legacy_would_reject = False
            try:
                ci = master_ci_status(repo, sha)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] sweep: CI lookup failed for "
                    f"{sha[:8]}: {exc} — proceeding to sm:done anyway"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                ci = {}
            conclusion = ci.get("conclusion")
            if conclusion not in (None, "pending", "success"):
                # The old code (pre-#784) would have labelled this
                # sm:rejected. Count the delta so operators can audit
                # the behaviour change via the done-line counter.
                legacy_would_reject = True
                report.sweep_merged_pr_legacy_would_reject += 1
            target_label = DONE_SM_LABEL
            reason = (
                f"closed-by-merge sweep: PR #{pr_number} merged at {sha} "
                f"({pr_url})"
            )
            if legacy_would_reject:
                run_url = ci.get("run_url") or "<unknown>"
                reason += (
                    f" [master CI was {conclusion!r} at merge time "
                    f"({run_url}); #784: PR-merge is authoritative, "
                    f"treating as sm:done]"
                )
        elif pr_state == "CLOSED":
            # Issue #784: don't flip to sm:rejected while a follow-up
            # PR for the same issue is still open — the work is in
            # flight on a second attempt, let the sweeper re-evaluate
            # next cadence once that PR lands or closes.
            if find_open_linked_prs is not None:
                try:
                    open_followups = find_open_linked_prs(repo, number)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] sweep: follow-up lookup failed for "
                        f"#{number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    open_followups = []
                open_followups = [
                    n for n in open_followups if n != pr_number
                ]
                if open_followups:
                    log(
                        f"[sm-dispatcher] sweep wait #{number}: "
                        f"PR #{pr_number} closed-unmerged but open follow-up "
                        f"PR(s) in flight: {open_followups!r}"
                    )
                    report.sweep_wait_followup_in_flight += 1
                    return
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
    is_reject_path = target_label == REJECTED_SM_LABEL
    # Issue #784 — safe-mode ONLY gates the REJECT path. The merged-PR
    # branch is unambiguous (merge is terminal) and must apply live
    # even while the operator is auditing REJECT decisions.
    reject_dry_run = is_reject_path and _safe_mode_enabled(sweeper_config)
    if dry_run or reject_dry_run:
        mode = "DRY-RUN" if dry_run else "SAFE-MODE"
        log(
            f"[sm-dispatcher] {mode} would sweep #{number}: "
            f"{stale_label} → {target_label} ({reason})"
        )
        report.swept += 1
        report.transitions.append((number, stale_label, target_label))
        if reject_dry_run and not dry_run:
            # Pure safe-mode suppression (not dispatcher-wide dry-run):
            # audit how many flips would have happened before the
            # operator enables live writes.
            report.sweep_would_reject += 1
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
