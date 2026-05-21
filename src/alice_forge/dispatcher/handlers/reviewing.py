"""Handler for ``sm:reviewing`` issues, plus the auto-rebase helper.

``_process_reviewing`` owns the merged-PR → ``sm:done`` (or rollback to ``sm:building`` on red CI) transitions. The CONFLICTING-PR path delegates to ``_handle_conflicting_pr`` for the three-tier auto-rebase / spawn / escalate dance (issue #173).
"""

from __future__ import annotations

from alice_forge.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_reviewing(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    close_issue: CloseIssueFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    pr_files: PRFilesFn | None,
    verify_pr: VerifyFn | None,
    post_merge_cleanup: PostMergeCleanupFn | None,
    pr_mergeable: "PRMergeableFn | None" = None,
    attempt_rebase: "Callable[[str], dict[str, Any]] | None" = None,
    spawn_rebase: "Callable[[dict[str, Any], str, str, str], str | None] | None" = None,
    has_live_spawn: "Callable[[int], bool] | None" = None,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda s: None,
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """T2 (reviewing → done) and T3 (reviewing → building) for one issue.

    ``post_merge_cleanup`` (Issue #127) is invoked after a successful
    ``reviewing → done`` transition with the merged PR's head branch and
    the issue number. ``None`` disables cleanup (the test default).

    ``verify_pr`` (Issue #128) is the smoke-test gate run between
    "CI-green" and the actual ``sm:done`` transition. ``None`` disables
    verification entirely (pre-#128 behavior — used by tests that
    don't want to stub the verifier). When non-None, the verifier is
    called with the linked PR number + its changed-file list (obtained
    via ``pr_files``); the verdict's ``outcome`` decides whether to
    proceed, skip-with-audit, or halt at ``sm:reviewing``.

    ``pr_mergeable`` / ``attempt_rebase`` / ``spawn_rebase`` /
    ``has_live_spawn`` (Issue #173) drive the auto-rebase handler on
    unmerged PRs at sm:reviewing. If the PR comes back ``CONFLICTING``,
    the dispatcher fires the three-tier rebase recovery (in-process
    rebase → fresh worker → escalation comment). All four arguments
    default to ``None`` — when any is unset the conflict handler is
    a no-op and the issue stays at sm:reviewing (pre-#173 behavior).
    """
    number = issue["number"]
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to look up PR for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is None:
        # No PR found at all — stay at reviewing. ``find_linked_pr``
        # queries ``--state all``, so this branch only fires when there
        # is genuinely no linked PR (deleted or never existed).
        # Surfaces are escalation-only.
        log(f"[sm-dispatcher] #{number} reviewing but no linked PR found — staying")
        return

    pr_number = pr.get("number")
    if not isinstance(pr_number, int):
        return
    try:
        merge_info = pr_merge_status(repo, pr_number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed merge-status for PR #{pr_number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    if not merge_info.get("merged"):
        # PR still open — check whether it's stuck on a merge conflict
        # and drive the Tier 1/2/3 auto-rebase handler. When the helper
        # callables aren't wired (e.g. tests that don't care about
        # conflicts), this stays a no-op.
        _handle_conflicting_pr(
            issue=issue,
            repo=repo,
            pr_number=pr_number,
            state=state,
            report=report,
            post_comment=post_comment,
            pr_mergeable=pr_mergeable,
            attempt_rebase=attempt_rebase,
            spawn_rebase=spawn_rebase,
            has_live_spawn=has_live_spawn,
            dry_run=dry_run,
            log=log,
            now_iso=now_iso,
        )
        return

    sha = merge_info.get("merge_commit_oid")
    pr_url = merge_info.get("pr_url") or pr.get("url") or "<unknown>"
    if not sha:
        log(f"[sm-dispatcher] #{number} PR merged but no merge_commit_oid — staying")
        return

    try:
        ci = master_ci_status(repo, sha)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed CI lookup for {sha[:8]}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    conclusion = ci.get("conclusion")
    if conclusion is None or conclusion == "pending":
        # No verdict yet — stay at reviewing for next pass.
        return

    if conclusion == "success":
        # ----- Issue #128 verification gate -----
        # CI green is necessary but not sufficient — run an
        # artifact-specific smoke test against the *actually-running*
        # system before declaring the issue done.
        verdict: dict[str, Any] | None = None
        if verify_pr is not None:
            files: list[str] = []
            if pr_files is not None:
                try:
                    files = pr_files(repo, pr_number)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to fetch PR files for "
                        f"#{pr_number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    # Without the file list we can't pick a recipe; bail
                    # this cadence and let the next poll retry. The
                    # issue stays at sm:reviewing.
                    return
            try:
                verdict = verify_pr(pr_number, files)
            except Exception as exc:  # noqa: BLE001 — verifier must never crash the loop
                log(
                    f"[sm-dispatcher] verifier raised for #{number}: "
                    f"{exc.__class__.__name__}: {exc} — treating as verify-failed"
                )
                verdict = {
                    "outcome": "fail",
                    "reason": f"verifier crashed: {exc.__class__.__name__}: {exc}",
                    "route": None,
                }
            outcome = (verdict or {}).get("outcome") or "fail"

            if outcome == "fail":
                v_reason = (verdict or {}).get("reason") or "verification failed"
                v_route = (verdict or {}).get("route")
                # Counter reflects "verifier returned fail this pass" —
                # incremented regardless of whether we actually post a
                # comment (dedup may suppress it). The operator's
                # done-line read of ``verify_failed=N`` should mean
                # "there are still N broken merges parked at reviewing"
                # rather than "we sent N comments to GH this cadence".
                report.verify_failed += 1
                report.verify_records.append((number, "fail", v_reason))
                verify_body = render_verify_comment(
                    "failed",
                    number,
                    reason=v_reason,
                    route=v_route,
                    timestamp=now_iso(),
                )
                if dry_run:
                    log(
                        f"[sm-dispatcher] DRY-RUN would post verify-failed on "
                        f"#{number}: {v_reason}"
                    )
                    return
                if state.has_verify_failed(number):
                    # Already posted this cadence-or-prior; don't spam.
                    # The label stays at sm:reviewing — a human inspects
                    # and either rolls back, escalates, or overrides.
                    log(
                        f"[sm-dispatcher] #{number} verify still failing "
                        f"({v_reason}) — comment already posted, staying"
                    )
                    return
                try:
                    post_comment(repo, number, verify_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to post verify-failed on "
                        f"#{number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    return
                state.mark_verify_failed(number)
                log(
                    f"[sm-dispatcher] #{number} verify-failed posted "
                    f"({v_reason}) — staying at sm:reviewing"
                )
                return

            # outcome == "pass" or "skip" — both allow the transition.
            # Post the audit comment first so the trail records *why*
            # we proceeded (pass means a probe succeeded; skip means
            # no recipe matched). If posting fails we still proceed —
            # the audit is best-effort, not gating.
            v_reason = (verdict or {}).get("reason") or ""
            v_route = (verdict or {}).get("route")
            verify_body = render_verify_comment(
                outcome,
                number,
                reason=v_reason,
                route=v_route,
                timestamp=now_iso(),
            )
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would post verify-{outcome} on "
                    f"#{number}: {v_reason}"
                )
            else:
                try:
                    post_comment(repo, number, verify_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to post verify-{outcome} on "
                        f"#{number}: {exc} — proceeding anyway"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
            if outcome == "pass":
                report.verify_pass += 1
            else:
                report.verify_skip += 1
            report.verify_records.append((number, outcome, v_reason))
            # If the issue had a prior verify-failed entry, clear it —
            # this cadence succeeded and the dedup ledger entry is
            # stale.
            state.clear_verify_failed(number)

        # ----- end verification gate -----

        reason = f"PR merged: {pr_url}, CI green on {sha}"
        body = render_transition_comment(REVIEWING_SM_LABEL, DONE_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"reviewing → done ({sha[:8]})"
            )
            report.transitioned += 1
            report.transitions.append((number, REVIEWING_SM_LABEL, DONE_SM_LABEL))
            return
        try:
            edit_labels(
                repo,
                number,
                add=[DONE_SM_LABEL],
                remove=[REVIEWING_SM_LABEL],
            )
            close_issue(repo, number)
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed close/transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, DONE_SM_LABEL))
        # Issue #173: a successful done transition closes any prior
        # CONFLICTING episode for this issue. Clear the dedup ledger so a
        # future re-entry into sm:reviewing (unlikely, but the state file
        # is long-lived) can fire Tier 1/2/3 again from scratch.
        state.clear_rebase_attempted(number)
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → done (closed)")
        # Issue #127 — restore the worker's working tree to master so the
        # next cycle doesn't read dispatcher.py from this departing
        # worker's feature branch. Cleanup is bounded to this exact
        # transition (merged + green); CI-red and unmerged-closed paths
        # never reach here.
        if post_merge_cleanup is not None:
            try:
                post_merge_cleanup(merge_info.get("head_ref_name"), number)
                report.cleaned_up += 1
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                log(
                    f"[sm-dispatcher] post-merge cleanup raised for #{number}: "
                    f"{exc!r}"
                )
        return

    if conclusion == "failure":
        run_url = ci.get("run_url") or "<unknown>"
        reason = f"CI red on merge: {run_url}"
        body = render_transition_comment(REVIEWING_SM_LABEL, BUILDING_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"reviewing → building (CI red {run_url})"
            )
            report.transitioned += 1
            report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
            return
        try:
            edit_labels(
                repo,
                number,
                add=[BUILDING_SM_LABEL],
                remove=[REVIEWING_SM_LABEL],
            )
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        # CI flipped red — the prior verify-failed entry (if any) was
        # for the green build that just regressed. Clear so when CI
        # eventually re-greens we don't suppress a fresh failure.
        state.clear_verify_failed(number)
        # Issue #173: a CI-red transition also closes the CONFLICTING
        # episode — the work moves back to sm:building and a fresh PR
        # may eventually open. Clear the ledger entry so the next
        # CONFLICTING incident starts fresh.
        state.clear_rebase_attempted(number)
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → building (CI red)")
        return


def _handle_conflicting_pr(
    *,
    issue: dict[str, Any],
    repo: str,
    pr_number: int,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    pr_mergeable: "PRMergeableFn | None",
    attempt_rebase: "Callable[[str], dict[str, Any]] | None",
    spawn_rebase: "Callable[[dict[str, Any], str, str, str], str | None] | None",
    has_live_spawn: "Callable[[int], bool] | None",
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """Issue #173 — Tier 1/2/3 auto-rebase handler for a CONFLICTING PR.

    Called from :func:`_process_reviewing` when the linked PR is still
    open. Looks up the GitHub-computed ``mergeable`` state and, if it
    is ``CONFLICTING``, runs the recovery ladder:

      * **Tier 1 (cheap)** — fire :func:`attempt_rebase`. On success
        post ``[SM] rebase-pushed`` and return; CI will re-fire on the
        new head and the dispatcher picks the PR up next cycle.
      * **Tier 2 (escalation)** — on rebase failure, post
        ``[SM] rebase-needed`` (with the offending file / stderr in the
        reason) AND spawn a fresh worker via :func:`spawn_rebase` to
        resolve conflicts manually. Marks the issue in
        ``state.rebase_attempted`` so a follow-up cycle can detect
        "the spawn died but the PR is still conflicting".
      * **Tier 3 (give up)** — if a prior Tier 2 spawn is dead (no live
        spawn dir) AND the PR is still CONFLICTING, post a
        ``[SM] rebase-escalated`` audit comment exactly once and stop
        retrying. Dedup'd by ``state.rebase_escalated_posted``.

    ``MERGEABLE`` and ``UNKNOWN`` results are no-ops — the existing
    worker self-merge path drives MERGEABLE, and UNKNOWN means GitHub
    is still computing so we wait. Any wiring callable left as ``None``
    short-circuits the handler (test/dry-run escape hatch).
    """
    number = issue["number"]
    if pr_mergeable is None or attempt_rebase is None or spawn_rebase is None:
        # Conflict handler isn't wired this run — silent no-op.
        return

    try:
        info = pr_mergeable(repo, pr_number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed mergeable lookup for PR #{pr_number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    mergeable = info.get("mergeable")
    if mergeable != "CONFLICTING":
        # MERGEABLE → wait for the worker's self-merge.
        # UNKNOWN → GH still computing, retry next cycle.
        # Anything else (None/odd) → treat as UNKNOWN.
        if mergeable in (None, "UNKNOWN"):
            log(
                f"[sm-dispatcher] #{number} PR #{pr_number} mergeable={mergeable!r} "
                f"— retry next cycle"
            )
        return

    branch = info.get("head_ref_name")
    if not branch:
        # Can't act without a branch name. Log and wait.
        log(
            f"[sm-dispatcher] #{number} PR #{pr_number} CONFLICTING but no "
            f"head_ref_name in gh payload — staying"
        )
        return

    # Already escalated to Tier 3 — stay silent until the operator
    # intervenes (either rebases manually, closes the PR, or flips the
    # state ledger entry by transitioning out of sm:reviewing).
    if state.has_rebase_escalated(number):
        log(
            f"[sm-dispatcher] #{number} CONFLICTING + already escalated — staying"
        )
        return

    # Tier 2 spawn already in flight — give it room to work.
    if has_live_spawn is not None and has_live_spawn(number):
        log(
            f"[sm-dispatcher] #{number} CONFLICTING — rebase spawn in flight, waiting"
        )
        return

    # Prior Tier 2 spawn is dead but the PR is still CONFLICTING → Tier 3.
    if state.has_rebase_attempted(number):
        reason = "spawned rebase worker dead but PR still CONFLICTING"
        body = render_rebase_escalation_comment(
            number, branch, reason, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would escalate rebase on "
                f"#{number} (branch={branch})"
            )
            report.rebase_escalated += 1
            report.rebase_records.append((number, "tier3-escalation", reason))
            return
        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] failed to post rebase escalation on "
                f"#{number}: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.mark_rebase_escalated(number)
        report.rebase_escalated += 1
        report.rebase_records.append((number, "tier3-escalation", reason))
        log(
            f"[sm-dispatcher] #{number} rebase escalation surfaced (Tier 3, "
            f"branch={branch})"
        )
        return

    # Tier 1 — cheap in-process rebase attempt.
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would attempt rebase on "
            f"#{number} (branch={branch})"
        )
        return

    result = attempt_rebase(branch)
    if result.get("ok"):
        report.rebase_pushed += 1
        reason = result.get("reason") or "rebased and pushed"
        report.rebase_records.append((number, "tier1-pushed", reason))
        body = render_rebase_pushed_audit_comment(
            number, branch, timestamp=now_iso()
        )
        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] rebase pushed on #{number} but audit "
                f"comment failed: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            # Non-fatal: the push already happened.
        log(
            f"[sm-dispatcher] #{number} auto-rebased and pushed branch={branch}"
        )
        return

    # Tier 2 — rebase failed. Post audit + spawn worker.
    reason = result.get("reason") or "auto-rebase failed"
    audit_body = render_rebase_needed_audit_comment(
        number, branch, reason, timestamp=now_iso()
    )
    try:
        post_comment(repo, number, audit_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post rebase-needed audit on "
            f"#{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    try:
        spawn_id = spawn_rebase(issue, repo, branch, reason)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to launch rebase spawn for "
            f"#{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        # Popen / filesystem errors: log + continue; the audit comment
        # was already posted so the cadence trail records the attempt.
        log(
            f"[sm-dispatcher] rebase spawn launch raised OSError on "
            f"#{number}: {exc}"
        )
        return

    if spawn_id is None:
        log(
            f"[sm-dispatcher] rebase spawn returned None for #{number} — "
            f"will retry next cycle"
        )
        return

    state.mark_rebase_attempted(number)
    report.rebase_spawned += 1
    report.rebase_records.append((number, "tier2-spawn", reason))
    log(
        f"[sm-dispatcher] #{number} rebase spawn launched ({spawn_id}, "
        f"branch={branch})"
    )
