"""Dispatcher main pass + CLI entrypoint.

:func:`run` orchestrates one cadence: poll for open ``sm:*`` issues, route each to its state handler, then sweep closed-stale and open-done issues. :func:`main` is the ``argparse`` wrapper invoked by ``bin/alice-sm-dispatcher`` (via ``python -m alice_forge.dispatcher``).
"""

from __future__ import annotations

from alice_forge.dispatcher.handlers._common import *  # noqa: F401, F403
from alice_forge.sm.ledger import EmittedLedger, load_or_migrate

# State-handler functions live in sibling modules under
# ``alice_forge.dispatcher.handlers``. They can't be re-exported via
# ``_common`` because the handler modules themselves import from
# ``_common`` — that would loop. Importing them explicitly here keeps
# the directed graph acyclic.
from alice_forge.dispatcher.handlers.building import _process_building
from alice_forge.dispatcher.handlers.compacting import _process_compacting
from alice_forge.dispatcher.handlers.design_review import _process_design_review
from alice_forge.dispatcher.handlers.designed import _process_designed
from alice_forge.dispatcher.handlers.designing import _process_designing
from alice_forge.dispatcher.handlers.draft import _process_draft
from alice_forge.dispatcher.handlers.needs_study import _process_needs_study
from alice_forge.dispatcher.handlers.open_done import _process_open_done
from alice_forge.dispatcher.handlers.reviewing import _process_reviewing
from alice_forge.dispatcher.handlers.selected import _process_selected
from alice_forge.dispatcher.handlers.stale_closed import _process_stale_closed


def _save_ledger_best_effort(
    ledger: EmittedLedger,
    ledger_path: pathlib.Path,
    log: Callable[[str], None],
) -> None:
    """Persist the v3 emit ledger; log + swallow on I/O failure.

    The dispatcher's primary contract is the v1 ``state_path`` save;
    the v3 ledger is shadowed alongside it during Phase 1 + 2 and a
    persistence failure here must NOT abort the cadence (which would
    drop the v1 state save too). When a v3 handler becomes
    load-bearing on the ledger (Phase 2 cutover per state), the
    error becomes a real failure mode; for now it's strictly
    best-effort.
    """
    try:
        ledger.save(ledger_path)
    except OSError as exc:
        log(
            f"[sm-dispatcher] failed to save v3 emit ledger at "
            f"{ledger_path}: {exc} (Phase 1: best-effort)"
        )


def _generate_cycle_id(now_iso_str: str) -> str:
    """Generate a stable per-cadence id for dual-run log pairing.

    Format: ``<utc-iso-second>-<8-hex>`` — enough resolution to
    distinguish overlapping cycles in tests and a hex suffix to
    avoid collisions if two repos happen to start a cycle in the
    same second. The id is opaque to the diff job; pair = same id.
    """
    import secrets

    return f"{now_iso_str}-{secrets.token_hex(4)}"


def _v3_dry_run(
    *,
    handler: Callable[..., Any],
    state_for_log: "SMState",
    issue: dict[str, Any],
    repo: str,
    cycle_id: str,
    ledger: EmittedLedger,
    list_comments: Callable[..., list[dict[str, Any]]],
    find_linked_pr: Callable[..., dict[str, Any] | None] | None = None,
    pr_merge_status: Callable[..., Any] | None = None,
    master_ci_status: Callable[..., Any] | None = None,
    research_resolver: Callable[[int], str | None] | None = None,
    trusted_authors: frozenset[str] = frozenset(),
    log_dir: pathlib.Path | None = None,
    now_iso: Callable[[], str] = lambda: "",
    log: Callable[[str], None] = lambda s: None,
) -> None:
    """Dual-run shim: invoke a v3 handler in dry-run mode.

    Generic across states. Caller supplies the handler function
    (``alice_forge.sm.handlers.<state>.handle``) and the
    :class:`SMState` value to record in the log. The shim renders
    the handler's :class:`HandlerResult` to
    ``v3-predicted.jsonl`` so the diff job can compare against v1's
    actual action on the same cycle.

    Failures inside the v3 handler are logged but never re-raised:
    Phase 2 dual-run must not destabilize v1's hot path.
    """
    if log_dir is None:
        return  # no log target configured; skip the dry-run entirely

    import datetime as _dt

    from alice_forge.sm.dual_run import log_entry as _log_entry
    from alice_forge.sm.services import HandlerServices

    number = issue.get("number")
    if not isinstance(number, int):
        return

    def _now_dt() -> _dt.datetime:
        try:
            return _dt.datetime.fromisoformat(now_iso())
        except ValueError:
            return _dt.datetime.now(_dt.timezone.utc)

    try:
        # Read-only IO (list_comments, find_linked_pr, ...) gets the
        # real callables so the handler sees true world state. Write
        # IO (post_comment, edit_labels, close_issue) is stubbed —
        # the dry-run must never modify GitHub.
        services = HandlerServices(
            ledger=ledger,
            repo=repo,
            post_comment=lambda *a, **kw: None,  # write — stubbed
            list_comments=list_comments,
            edit_labels=lambda *a, **kw: None,  # write — stubbed
            close_issue=lambda *a, **kw: None,  # write — stubbed
            find_linked_pr=find_linked_pr if find_linked_pr else lambda *a, **kw: None,
            pr_merge_status=pr_merge_status if pr_merge_status else lambda *a, **kw: None,
            master_ci_status=master_ci_status if master_ci_status else lambda *a, **kw: None,
            trusted_authors=trusted_authors,
            now=_now_dt,
            log=log,
            research_resolver=research_resolver,
        )
        result = handler(issue, services)
    except Exception as exc:  # noqa: BLE001 — defense for v3 bugs
        log(
            f"[sm-v3] dry-run {handler.__module__}.{handler.__name__} "
            f"#{number} raised: {type(exc).__name__}: {exc}"
        )
        return

    try:
        _log_entry(
            log_dir / "sm-v3-predicted.jsonl",
            cycle_id=cycle_id,
            lane="v3-predicted",
            repo=repo,
            issue_number=number,
            state=state_for_log,
            result=result,
            now=_now_dt(),
        )
    except OSError as exc:
        log(
            f"[sm-v3] failed to write v3-predicted log entry for "
            f"#{number}: {exc} (Phase 2: best-effort)"
        )


def run(
    *,
    repo: str = DEFAULT_REPO,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path | None = None,
    v3_dry_run_states: frozenset[str] = frozenset(),
    v3_dry_run_log_dir: pathlib.Path | None = None,
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
    labels_configured: bool = True,
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

    # SM v3 Phase 1: load the unified emit ledger alongside v1's
    # DispatcherState. v1 handlers continue to use ``state``; v3
    # handlers (Phase 2 onward) will read+write ``ledger``. Until any
    # v3 handler is wired, the ledger is loaded and saved but
    # otherwise unused — the round-trip exercises the persistence
    # path on every cadence so a corrupt file surfaces immediately
    # rather than at first Phase 2 deploy.
    if ledger_path is None:
        ledger_path = state_path.parent / "sm-emit-ledger.json"
    try:
        ledger = load_or_migrate(ledger_path, v1_state_path=state_path)
    except (OSError, ValueError) as exc:
        log(
            f"[sm-dispatcher] failed to load v3 emit ledger at "
            f"{ledger_path}: {exc} — starting empty"
        )
        ledger = EmittedLedger()

    report.polled = len(issues)

    # SM v3 Phase 2: stable per-cadence id used to pair v1-actual and
    # v3-predicted log entries in the dual-run diff job. Generated
    # once per run() call; every issue processed in this cadence
    # shares the same cycle_id.
    _cycle_id = _generate_cycle_id(now_iso())

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
            #
            # Issue #261 — relaxed mode: when this repo isn't on the
            # SM v2 label taxonomy (``labels_configured=False``, set
            # in ``alice.config.json``), the label gate is bypassed.
            # The dispatcher silently skips issues without canonical
            # labels rather than logging a noisy "expected exactly
            # one sm:* label" rejection for every cozyhem-engine
            # ticket. Labels stay a Speaking/Thinking convenience,
            # not a gate.
            if not labels_configured:
                continue
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
                # SM v3 Phase 2.4: dual-run the v3 needs_study handler.
                if NEEDS_STUDY_SM_LABEL in v3_dry_run_states:
                    from alice_forge.sm.handlers.needs_study import handle as _h_ns
                    from alice_forge.sm.states import SMState as _SMState

                    def _v3_resolve(n: int) -> str | None:
                        try:
                            note = _find_resolving_research_note(n, research_dir)
                        except Exception:
                            return None
                        return note.stem if note is not None else None

                    _v3_dry_run(
                        handler=_h_ns,
                        state_for_log=_SMState.NEEDS_STUDY,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        list_comments=list_comments,
                        research_resolver=_v3_resolve,
                        trusted_authors=trusted_authors,
                        log_dir=v3_dry_run_log_dir,
                        now_iso=now_iso,
                        log=log,
                    )
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
                # SM v3 Phase 2.1: dual-run the v3 draft handler in
                # dry-run mode when this state is in the flag set.
                # v3 just logs its predicted action; v1 still applies
                # the actual side-effects. The diff job (separate
                # tool) compares the two streams.
                if DRAFT_SM_LABEL in v3_dry_run_states:
                    from alice_forge.sm.handlers.draft import handle as _h_draft
                    from alice_forge.sm.states import SMState as _SMState
                    _v3_dry_run(
                        handler=_h_draft,
                        state_for_log=_SMState.DRAFT,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_dry_run_log_dir,
                        now_iso=now_iso,
                        log=log,
                    )
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
                # SM v3 Phase 2.2: dual-run the v3 compacting handler.
                if COMPACTING_SM_LABEL in v3_dry_run_states:
                    from alice_forge.sm.handlers.compacting import handle as _h_compacting
                    from alice_forge.sm.states import SMState as _SMState
                    _v3_dry_run(
                        handler=_h_compacting,
                        state_for_log=_SMState.COMPACTING,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_dry_run_log_dir,
                        now_iso=now_iso,
                        log=log,
                    )
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
                # SM v3 Phase 2.3: dual-run the v3 building handler.
                if BUILDING_SM_LABEL in v3_dry_run_states:
                    from alice_forge.sm.handlers.building import handle as _h_building
                    from alice_forge.sm.states import SMState as _SMState
                    _v3_dry_run(
                        handler=_h_building,
                        state_for_log=_SMState.BUILDING,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        list_comments=list_comments,
                        find_linked_pr=find_linked_pr,
                        trusted_authors=trusted_authors,
                        log_dir=v3_dry_run_log_dir,
                        now_iso=now_iso,
                        log=log,
                    )
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
            _save_ledger_best_effort(ledger, ledger_path, log)
        return 1, report

    if not dry_run:
        save_state(state_path, state)
        _save_ledger_best_effort(ledger, ledger_path, log)

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
        default=None,
        help=(
            "GitHub repo in <org>/<name> form. Single-repo override — when "
            "set, the dispatcher runs one pass against this repo only and "
            "ignores the sm_dispatcher.repos config block. When omitted, "
            "the dispatcher iterates over every repo configured in "
            f"alice.config.json (default: {DEFAULT_REPO} when no config)."
        ),
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

    state_path = pathlib.Path(args.state)
    log = lambda s: print(s, file=sys.stderr)  # noqa: E731

    # Issue #261 — when ``--repo`` is set, behave like pre-#261: one pass
    # against the named repo with the legacy ``WORKER_REPO_PATH`` checkout
    # and ``labels_configured=True`` (alice's contract). When unset, read
    # the multi-repo config and iterate.
    if args.repo is not None:
        repos = [
            RepoConfig(
                slug=args.repo,
                checkout_path=WORKER_REPO_PATH,
                labels_configured=True,
            )
        ]
    else:
        repos = load_dispatcher_repos(log=log)

    worst_exit = 0
    for repo_cfg in repos:
        if not repo_cfg.checkout_path.is_dir():
            # Config can point at a checkout that hasn't been cloned yet
            # (e.g. cozyhem-engine on a fresh worker). Log + skip rather
            # than crashing the whole loop — other repos in the list
            # must still process.
            log(
                f"[sm-dispatcher] skipping {repo_cfg.slug}: checkout path "
                f"{repo_cfg.checkout_path} does not exist"
            )
            continue
        try:
            exit_code, _ = run(
                repo=repo_cfg.slug,
                state_path=state_path,
                worker_repo_path=repo_cfg.checkout_path,
                labels_configured=repo_cfg.labels_configured,
                dry_run=args.dry_run,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001 — defense for one-repo failures
            # An unhandled exception in one repo's pass must not abort
            # the remaining repos. Log + flag a non-zero exit so the
            # supervisor sees the failure on the next cadence.
            log(f"[sm-dispatcher] {repo_cfg.slug} pass crashed: {exc!r}")
            worst_exit = max(worst_exit, 1)
            continue
        worst_exit = max(worst_exit, exit_code)
    return worst_exit
