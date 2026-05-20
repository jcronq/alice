"""Dispatcher main pass + CLI entrypoint.

:func:`run` orchestrates one cadence: poll for open ``sm:*`` issues, route each to its state handler, then sweep closed-stale and open-done issues. :func:`main` is the ``argparse`` wrapper invoked by ``bin/alice-sm-dispatcher`` (via ``python -m sm.dispatcher``).
"""

from __future__ import annotations

from sm.dispatcher.handlers._common import *  # noqa: F401, F403

# State-handler functions live in sibling modules under
# ``sm.dispatcher.handlers``. They can't be re-exported via
# ``_common`` because the handler modules themselves import from
# ``_common`` — that would loop. Importing them explicitly here keeps
# the directed graph acyclic.
from sm.dispatcher.handlers.building import _process_building
from sm.dispatcher.handlers.compacting import _process_compacting
from sm.dispatcher.handlers.design_review import _process_design_review
from sm.dispatcher.handlers.designed import _process_designed
from sm.dispatcher.handlers.designing import _process_designing
from sm.dispatcher.handlers.draft import _process_draft
from sm.dispatcher.handlers.needs_study import _process_needs_study
from sm.dispatcher.handlers.open_done import _process_open_done
from sm.dispatcher.handlers.reviewing import _process_reviewing
from sm.dispatcher.handlers.selected import _process_selected
from sm.dispatcher.handlers.stale_closed import _process_stale_closed


def run(
    *,
    repo: str = DEFAULT_REPO,
    state_path: pathlib.Path,
    list_issues: ListIssuesFn | None = None,
    list_stale_closed: ListIssuesFn | None = None,
    list_open_done: ListIssuesFn | None = None,
    post_comment: PostCommentFn = gh_post_comment,
    edit_labels: EditLabelsFn = gh_edit_labels,
    close_issue: CloseIssueFn = gh_close_issue,
    find_linked_pr: FindLinkedPRFn = gh_find_linked_pr,
    pr_merge_status: PRMergeStatusFn = gh_get_pr_merge_status,
    pr_mergeable: PRMergeableFn | None = None,
    master_ci_status: MasterCIStatusFn = gh_get_master_ci_status,
    has_live_spawn: Callable[[int], bool] | None = None,
    live_spawn_dir: Callable[[int], pathlib.Path | None] | None = None,
    count_running: Callable[[], int] | None = None,
    spawn: Callable[[dict[str, Any], str, str], str | None] | None = None,
    has_live_thinking_spawn: Callable[[int], bool] | None = None,
    count_running_thinking: Callable[[], int] | None = None,
    spawn_thinking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    has_live_speaking_spawn: Callable[[int], bool] | None = None,
    count_running_speaking: Callable[[], int] | None = None,
    spawn_speaking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    spawn_rebase: Callable[[dict[str, Any], str, str, str], str | None] | None = None,
    attempt_rebase: Callable[[str], dict[str, Any]] | None = None,
    enable_rebase: bool = True,
    get_issue: Callable[[int], dict[str, Any] | None] | None = None,
    proactive_reap: Callable[[], tuple[int, int]] | None = None,
    enable_spawn: bool = True,
    max_concurrent_spawns: int = MAX_CONCURRENT_SPAWNS,
    max_concurrent_thinking_spawns: int = MAX_CONCURRENT_THINKING_SPAWNS,
    max_concurrent_speaking_spawns: int = MAX_CONCURRENT_SPEAKING_SPAWNS,
    post_merge_cleanup: PostMergeCleanupFn | None = None,
    enable_cleanup: bool = True,
    worker_repo_path: pathlib.Path = WORKER_REPO_PATH,
    pr_files: PRFilesFn | None = None,
    verify_pr: VerifyFn | None = None,
    enable_verify: bool = True,
    list_comments: ListCommentsFn | None = None,
    notes_dir: pathlib.Path = NEEDS_STUDY_HINT_DIR,
    research_dir: pathlib.Path = RESEARCH_NOTES_DIR,
    triage_surface_dir: pathlib.Path = TRIAGE_SURFACE_DIR,
    triage_surface_body_char_limit: int = TRIAGE_SURFACE_BODY_CHAR_LIMIT,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    now_iso: Callable[[], str] = _now_iso,
) -> tuple[int, RunReport]:
    """Run one dispatcher pass. Returns ``(exit_code, report)``.

    Exit codes:
      0  poll completed (zero or more comments posted; state saved)
      1  ``gh`` failed in a way we can't recover from this pass —
         auth, rate limit, transport error. State NOT written;
         s6 supervisor will retry on the next cadence.
    """
    if list_issues is None:
        list_issues = gh_list_sm_issues
    if list_stale_closed is None:
        list_stale_closed = gh_list_stale_closed_sm_issues
    if list_open_done is None:
        list_open_done = gh_list_open_done_sm_issues
    if list_comments is None:
        list_comments = gh_list_issue_comments
    if enable_spawn:
        # Default to live production wiring when the caller hasn't
        # provided test fixtures. enable_spawn=False is the test escape
        # hatch — leaves has_live_spawn / count_running / spawn as
        # None, so :func:`_process_selected` short-circuits the spawn
        # branch.
        if has_live_spawn is None:
            def has_live_spawn(number: int) -> bool:
                return has_live_spawn_for_issue(number, SPAWN_DIR, log=log)
        if live_spawn_dir is None:
            def live_spawn_dir(number: int) -> pathlib.Path | None:
                return find_live_spawn_dir_for_issue(number, SPAWN_DIR)
        if count_running is None:
            def count_running() -> int:
                return count_running_spawns(SPAWN_DIR, log=log)
        if spawn is None:
            def spawn(
                issue: dict[str, Any], art_label: str, repo: str
            ) -> str | None:
                return spawn_agent(
                    issue,
                    art_label,
                    repo,
                    post_comment=post_comment,
                    log=log,
                    now_iso=now_iso,
                )
        if get_issue is None:
            def get_issue(number: int) -> dict[str, Any] | None:
                return gh_get_issue(repo, number)
        if proactive_reap is None:
            def proactive_reap() -> tuple[int, int]:
                return proactive_reap_dead_spawns(
                    SPAWN_DIR, get_issue=get_issue, log=log
                )
        # Sub-issue 7 (#186): SM v2 thinking + speaking lane bindings.
        # Each lane has its own spawn dir, concurrency cap, and audit
        # prefix so they don't share dedup / capacity with the v1
        # worker pool.
        if has_live_thinking_spawn is None:
            def has_live_thinking_spawn(number: int) -> bool:
                return has_live_thinking_spawn_for_issue(
                    number, SM_THINKING_SPAWN_DIR, log=log
                )
        if count_running_thinking is None:
            def count_running_thinking() -> int:
                return count_running_thinking_spawns(
                    SM_THINKING_SPAWN_DIR, log=log
                )
        if spawn_thinking is None:
            def spawn_thinking(
                issue: dict[str, Any], art_label: str, repo: str
            ) -> str | None:
                return spawn_thinking_agent(
                    issue,
                    art_label,
                    repo,
                    post_comment=post_comment,
                    log=log,
                    now_iso=now_iso,
                )
        if has_live_speaking_spawn is None:
            def has_live_speaking_spawn(number: int) -> bool:
                return has_live_speaking_spawn_for_issue(
                    number, SM_SPEAKING_SPAWN_DIR, log=log
                )
        if count_running_speaking is None:
            def count_running_speaking() -> int:
                return count_running_speaking_spawns(
                    SM_SPEAKING_SPAWN_DIR, log=log
                )
        if spawn_speaking is None:
            def spawn_speaking(
                issue: dict[str, Any], art_label: str, repo: str
            ) -> str | None:
                return spawn_speaking_agent(
                    issue,
                    art_label,
                    repo,
                    post_comment=post_comment,
                    log=log,
                    now_iso=now_iso,
                )

    # Issue #127 — bind the production cleanup callable when enabled and
    # not explicitly injected. Tests opt out with ``enable_cleanup=False``
    # (mirrors the ``enable_spawn=False`` escape hatch) or pass a fake.
    if enable_cleanup and post_merge_cleanup is None and not dry_run:
        def post_merge_cleanup(branch: str | None, issue_number: int) -> None:
            _post_merge_cleanup(
                repo_path=worker_repo_path,
                branch=branch,
                issue_number=issue_number,
                log=log,
            )

    # Issue #128 — bind the production verifier + PR-files fetcher when
    # the caller hasn't injected fakes. ``enable_verify=False`` and the
    # ``ALICE_VERIFY_ENABLED`` env var both flip the gate off, in which
    # case ``_process_reviewing`` receives ``verify_pr=None`` and goes
    # straight from CI-green to ``sm:done`` (pre-#128 behavior). The
    # env-var path is the operational kill-switch; the kwarg path is
    # the test escape hatch.
    if enable_verify and verify_pr is None and _verify_enabled():
        if pr_files is None:
            pr_files = gh_get_pr_files
        verify_pr = default_verifier
    elif not enable_verify or not _verify_enabled():
        # Operator/test explicitly disabled — None signals "skip the
        # whole gate" to ``_process_reviewing``.
        verify_pr = None

    # Issue #173 — bind the production auto-rebase callables. The
    # ``enable_rebase=False`` flag and the absence of an injected
    # ``spawn_rebase`` (with ``enable_spawn=False``) both leave the
    # CONFLICTING handler a silent no-op, matching the existing test
    # escape-hatch shape for ``_process_reviewing``.
    if enable_rebase and not dry_run:
        if pr_mergeable is None:
            pr_mergeable = gh_get_pr_mergeable
        if attempt_rebase is None:
            def attempt_rebase(branch: str) -> dict[str, Any]:
                return _attempt_auto_rebase(
                    branch=branch,
                    repo_path=worker_repo_path,
                    log=log,
                )
        if enable_spawn and spawn_rebase is None:
            def spawn_rebase(
                issue: dict[str, Any],
                repo: str,
                branch: str,
                reason: str,
            ) -> str | None:
                return spawn_rebase_agent(
                    issue,
                    repo,
                    branch,
                    reason,
                    log=log,
                )
    else:
        # Disabled: leave all three None so _handle_conflicting_pr no-ops.
        pr_mergeable = None
        attempt_rebase = None
        spawn_rebase = None

    report = RunReport()

    # Issue #142 — proactive sweep of stale ``active/`` spawn dirs.
    # Without this, dead dirs only get reaped when a new spawn for the
    # same issue fires (via ``has_live_spawn_for_issue``), so they
    # accumulate visibly in /running and /runs after their issue closes.
    # Best-effort: a failure here must not block the main poll.
    if proactive_reap is not None:
        try:
            proactive_reap()
        except OSError as exc:
            log(f"[sm-dispatcher] proactive-reap failed: {exc}")

    try:
        issues = list_issues(repo)
    except GHCommandError as exc:
        if exc.looks_like_auth_failure:
            log(f"[sm-dispatcher] auth failure listing {repo}: {exc}")
        elif exc.looks_like_rate_limit:
            log(f"[sm-dispatcher] rate-limited listing {repo}: {exc}")
        else:
            log(f"[sm-dispatcher] failed to list {repo}: {exc}")
        # Do NOT write partial state. The s6 supervisor retries.
        return 1, report

    state = load_state(state_path)
    report.polled = len(issues)

    fatal_exit = False
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            log(f"[sm-dispatcher] skipping issue with non-integer number: {number!r}")
            continue

        sm_label = _current_sm_label(issue)
        if sm_label is None:
            # Either zero or >1 whitelisted ``sm:*`` labels (or only
            # non-canonical ones like ``sm:bogus``). Treated as a
            # trust-filter rejection — same v0 semantics, just hoisted
            # to the outer loop now that we route by label.
            names = _label_names(issue)
            sm_labels_seen = [n for n in names if n.startswith("sm:")]
            log(
                f"[sm-dispatcher] skipping #{number}: "
                f"expected exactly one whitelisted sm:* label, got {sm_labels_seen!r}"
            )
            report.skipped_trust += 1
            continue

        try:
            if sm_label == ACTIVE_SM_LABEL:
                _process_selected(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    has_live_spawn=has_live_spawn,
                    count_running=count_running,
                    spawn=spawn,
                    max_concurrent_spawns=max_concurrent_spawns,
                    has_live_thinking_spawn=has_live_thinking_spawn,
                    count_running_thinking=count_running_thinking,
                    spawn_thinking=spawn_thinking,
                    max_concurrent_thinking_spawns=max_concurrent_thinking_spawns,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                    get_issue=get_issue,
                )
            elif sm_label == REVIEWING_SM_LABEL:
                _process_reviewing(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    close_issue=close_issue,
                    find_linked_pr=find_linked_pr,
                    pr_merge_status=pr_merge_status,
                    master_ci_status=master_ci_status,
                    pr_files=pr_files,
                    verify_pr=verify_pr,
                    post_merge_cleanup=post_merge_cleanup,
                    pr_mergeable=pr_mergeable,
                    attempt_rebase=attempt_rebase,
                    spawn_rebase=spawn_rebase,
                    has_live_spawn=has_live_spawn,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == NEEDS_STUDY_SM_LABEL:
                _process_needs_study(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    notes_dir=notes_dir,
                    research_dir=research_dir,
                    trusted_authors=trusted_authors,
                    art_whitelist=ART_LABEL_WHITELIST,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DRAFT_SM_LABEL:
                _process_draft(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    art_whitelist=ART_LABEL_WHITELIST,
                    surface_dir=triage_surface_dir,
                    body_char_limit=triage_surface_body_char_limit,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DESIGNING_SM_LABEL:
                _process_designing(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DESIGN_REVIEW_SM_LABEL:
                _process_design_review(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DESIGNED_SM_LABEL:
                _process_designed(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    live_spawn_dir=live_spawn_dir,
                    has_live_speaking_spawn=has_live_speaking_spawn,
                    count_running_speaking=count_running_speaking,
                    spawn_speaking=spawn_speaking,
                    max_concurrent_speaking_spawns=max_concurrent_speaking_spawns,
                    dry_run=dry_run,
                    log=log,
                )
            elif sm_label == COMPACTING_SM_LABEL:
                _process_compacting(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    has_live_spawn=has_live_spawn,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                )
            elif sm_label == BUILDING_SM_LABEL:
                _process_building(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    dry_run=dry_run,
                    log=log,
                )
            else:
                # Phase 1.5 doesn't act on validating / done / rejected /
                # blocked. Listed for visibility only.
                log(f"[sm-dispatcher] #{number} at {sm_label} — no action this phase")
        except GHCommandError as exc:
            # Auth/rate-limit re-raised from inner handlers — bail.
            fatal_exit = True
            log(f"[sm-dispatcher] fatal gh error: {exc}")
            break

    # Phase 1.6 — sweep pass: catch closed issues that still carry a
    # non-terminal ``sm:*`` label and route them to a terminal state.
    # Runs only if the open-issue pass didn't bail with a fatal gh
    # error; the sweep is best-effort and shouldn't override a fatal
    # signal from the primary poll.
    if not fatal_exit:
        try:
            stale_issues = list_stale_closed(repo)
        except GHCommandError as exc:
            # The sweep is a defense-in-depth pass; failing to list
            # closed issues is not fatal to the primary loop. Log and
            # continue so dedup state still saves.
            if exc.looks_like_auth_failure:
                log(f"[sm-dispatcher] sweep auth failure listing {repo}: {exc}")
                fatal_exit = True
            elif exc.looks_like_rate_limit:
                log(f"[sm-dispatcher] sweep rate-limited listing {repo}: {exc}")
                fatal_exit = True
            else:
                log(f"[sm-dispatcher] sweep failed to list closed {repo}: {exc}")
            stale_issues = []
        for issue in stale_issues:
            number = issue.get("number")
            if not isinstance(number, int):
                log(
                    f"[sm-dispatcher] sweep skip issue with non-integer "
                    f"number: {number!r}"
                )
                continue
            try:
                _process_stale_closed(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    pr_merge_status=pr_merge_status,
                    master_ci_status=master_ci_status,
                    dry_run=dry_run,
                    log=log,
                )
            except GHCommandError as exc:
                fatal_exit = True
                log(f"[sm-dispatcher] fatal gh error during sweep: {exc}")
                break

    # Issue #174 — open-done sweep: OPEN issues at ``sm:done`` are the
    # art:research_note close-stragglers. The worker flipped the label
    # but no ``gh issue close`` ever fired (no PR pedigree means
    # ``_process_reviewing`` never owned the close). The handler
    # enforces the ``[SM] exit-transition`` gate and closes the issue.
    # Best-effort, same as the closed-stale sweep.
    if not fatal_exit:
        try:
            open_done_issues = list_open_done(repo)
        except GHCommandError as exc:
            if exc.looks_like_auth_failure:
                log(
                    f"[sm-dispatcher] open-done sweep auth failure listing "
                    f"{repo}: {exc}"
                )
                fatal_exit = True
            elif exc.looks_like_rate_limit:
                log(
                    f"[sm-dispatcher] open-done sweep rate-limited listing "
                    f"{repo}: {exc}"
                )
                fatal_exit = True
            else:
                log(
                    f"[sm-dispatcher] open-done sweep failed to list "
                    f"{repo}: {exc}"
                )
            open_done_issues = []
        for issue in open_done_issues:
            number = issue.get("number")
            if not isinstance(number, int):
                log(
                    f"[sm-dispatcher] open-done sweep skip issue with non-integer "
                    f"number: {number!r}"
                )
                continue
            try:
                _process_open_done(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    close_issue=close_issue,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            except GHCommandError as exc:
                fatal_exit = True
                log(
                    f"[sm-dispatcher] fatal gh error during open-done sweep: {exc}"
                )
                break

    if fatal_exit:
        # Persist what we did manage so dedup state for any successful
        # hello posts isn't lost.
        if not dry_run:
            save_state(state_path, state)
        return 1, report

    if not dry_run:
        save_state(state_path, state)

    log(
        f"[sm-dispatcher] done — polled={report.polled} "
        f"posted={report.posted} "
        f"transitioned={report.transitioned} "
        f"swept={report.swept} "
        f"spawned={report.spawned} "
        f"hinted={report.hinted} "
        f"cleaned_up={report.cleaned_up} "
        f"verify_pass={report.verify_pass} "
        f"verify_skip={report.verify_skip} "
        f"verify_failed={report.verify_failed} "
        f"rebase_pushed={report.rebase_pushed} "
        f"rebase_spawned={report.rebase_spawned} "
        f"rebase_escalated={report.rebase_escalated} "
        f"research_closed={report.research_closed} "
        f"exit_required={report.exit_required_posted} "
        f"skipped_dedup={report.skipped_dedup} "
        f"skipped_trust={report.skipped_trust}"
    )
    return 0, report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One pass of the State Machine v0/v1.5/v2 dispatcher."
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"GitHub repo in <org>/<name> form (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_DIR / DEFAULT_STATE_FILE),
        help="path to sm-dispatcher-state.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the comments/transitions that would be made, "
        "don't touch GitHub or state",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    exit_code, _ = run(
        repo=args.repo,
        state_path=pathlib.Path(args.state),
        dry_run=args.dry_run,
    )
    return exit_code
