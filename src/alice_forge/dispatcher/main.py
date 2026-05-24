"""Dispatcher main pass + CLI entrypoint.

:func:`run` orchestrates one cadence: poll for open ``sm:*`` issues, route each to its state handler, then sweep closed-stale and open-done issues. :func:`main` is the ``argparse`` wrapper invoked by ``bin/alice-sm-dispatcher`` (via ``python -m alice_forge.dispatcher``).
"""

from __future__ import annotations

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403
from alice_forge.sm.ledger import EmittedLedger, load_or_migrate
from alice_forge.sm.states import SMState
from alice_forge.sm.enforcement import (
    grace_pass_over_issues as _v3_grace_pass,
    is_enforcement_enabled as _v3_enforcement_enabled,
)

# v1 ``_process_*`` handlers — Phase 4 (#301) moved to
# :mod:`alice_forge.sm.legacy.handlers`. The dispatcher still drives
# them as the fallback for side-effects v3 hasn't ported (spawn
# dispatch, hello, rebase machinery, verify, post-merge cleanup).
# v3 (:mod:`alice_forge.sm.handlers`) now owns transition decisions;
# when v3 returns a :class:`alice_forge.sm.result.Transition`, the
# matching legacy handler is skipped for that issue this cadence to
# avoid double-emitting label edits and audit comments.
from alice_forge.sm.legacy.handlers.building import _process_building
from alice_forge.sm.legacy.handlers.compacting import _process_compacting
from alice_forge.sm.legacy.handlers.design_review import _process_design_review
from alice_forge.sm.legacy.handlers.designed import _process_designed
from alice_forge.sm.legacy.handlers.designing import _process_designing
from alice_forge.sm.legacy.handlers.draft import _process_draft
from alice_forge.sm.legacy.handlers.needs_study import _process_needs_study
from alice_forge.sm.legacy.handlers.open_done import _process_open_done
from alice_forge.sm.legacy.handlers.reviewing import _process_reviewing
from alice_forge.sm.legacy.handlers.selected import _process_selected
from alice_forge.sm.legacy.handlers.stale_closed import _process_stale_closed


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


def _v3_run(
    *,
    handler: Callable[..., Any],
    state_for_log: "SMState",
    issue: dict[str, Any],
    repo: str,
    cycle_id: str,
    ledger: EmittedLedger,
    post_comment: Callable[..., None],
    edit_labels: Callable[..., None],
    close_issue: Callable[..., None],
    list_comments: Callable[..., list[dict[str, Any]]],
    find_linked_pr: Callable[..., dict[str, Any] | None] | None = None,
    pr_merge_status: Callable[..., Any] | None = None,
    master_ci_status: Callable[..., Any] | None = None,
    research_resolver: Callable[[int], str | None] | None = None,
    trusted_authors: frozenset[str] = frozenset(),
    log_dir: pathlib.Path | None = None,
    dry_run: bool = False,
    now_iso: Callable[[], str] = lambda: "",
    log: Callable[[str], None] = lambda s: None,
) -> bool:
    """Run a v3 handler with real services and apply its result.

    Phase 4 (#301) flipped this from dry-run / dual-run shadow to
    authoritative. Where the handler returns a
    :class:`alice_forge.sm.result.HandlerResult`, the matching
    side-effect (label edit, audit comment, ledger write, parse-
    error reply) is applied immediately via
    :func:`alice_forge.sm.apply.apply_result`. The dual-run logger
    keeps writing entries — now on the ``v3-actual`` lane — for one
    month so the previous shadow-comparison output still parses.

    Returns ``True`` if a transition was applied (label changed) so
    the caller can skip the legacy v1 handler for this cadence;
    ``False`` otherwise. Returning ``False`` means v1 still gets a
    chance to do its non-transitioning work (spawn dispatch, hello,
    rebase machinery, verify gate, post-merge cleanup) — those
    side-effects are not yet ported to v3.

    Failures inside the v3 handler are logged and swallowed: a v3
    bug must not destabilise the v1 fallback path during the grace
    period.
    """
    import datetime as _dt

    from alice_forge.sm.apply import apply_result
    from alice_forge.sm.dual_run import log_entry as _log_entry
    from alice_forge.sm.services import HandlerServices

    number = issue.get("number")
    if not isinstance(number, int):
        return False

    def _now_dt() -> _dt.datetime:
        try:
            return _dt.datetime.fromisoformat(now_iso())
        except ValueError:
            return _dt.datetime.now(_dt.timezone.utc)

    # ``dry_run`` (CLI flag / test mode) stubs every write IO so the
    # handler can be exercised without touching GitHub. Production
    # runs pass the real callables.
    def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    if dry_run:
        services_post_comment: Callable[..., None] = _noop
        services_edit_labels: Callable[..., None] = _noop
        services_close_issue: Callable[..., None] = _noop
    else:
        services_post_comment = post_comment
        services_edit_labels = edit_labels
        services_close_issue = close_issue

    try:
        services = HandlerServices(
            ledger=ledger,
            repo=repo,
            post_comment=services_post_comment,
            list_comments=list_comments,
            edit_labels=services_edit_labels,
            close_issue=services_close_issue,
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
            f"[sm-v3] {handler.__module__}.{handler.__name__} "
            f"#{number} raised: {type(exc).__name__}: {exc}"
        )
        return False

    transitioned = False
    if result is not None:
        try:
            transitioned = apply_result(
                issue=issue,
                current_state=state_for_log,
                result=result,
                services=services,
            )
        except Exception as exc:  # noqa: BLE001 — defense for apply bugs
            log(
                f"[sm-v3] apply_result #{number} at {state_for_log.value} "
                f"raised: {type(exc).__name__}: {exc}"
            )
            transitioned = False

    if log_dir is not None:
        try:
            _log_entry(
                log_dir / "sm-v3-actual.jsonl",
                cycle_id=cycle_id,
                lane="v3-actual",
                repo=repo,
                issue_number=number,
                state=state_for_log,
                result=result,
                now=_now_dt(),
            )
        except OSError as exc:
            log(
                f"[sm-v3] failed to write v3-actual log entry for "
                f"#{number}: {exc} (Phase 4: best-effort)"
            )

    return transitioned


def _apply_v3_grace_pass(
    *,
    issues: list[dict[str, Any]],
    repo: str,
    ledger: EmittedLedger,
    edit_labels: Callable[..., None],
    post_comment: Callable[[str, int, str], None],
    dry_run: bool,
    now_iso: Callable[[], str],
    log: Callable[[str], None],
) -> None:
    """One-time grace transition pass — Phase 3.

    When ``SM_REQUIRE_CONTINUE=1`` flips ON, in-flight issues whose
    last activity is older than the new TTL get a one-time transition
    to ``sm:blocked``. Idempotent via the ledger's
    ``grace-transition`` side-effect — re-running the pass against
    an already-graced issue is a no-op.

    Best-effort: a failure here must NOT abort the main poll. The
    enforcement is additive; the worst case is the issue waits one
    more cadence for the grace block to fire.
    """
    import datetime as _dt

    try:
        now = _dt.datetime.fromisoformat(now_iso())
    except ValueError:
        now = _dt.datetime.now(_dt.timezone.utc)

    triples: list[tuple[int, SMState, _dt.datetime]] = []
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        label = _current_sm_label(issue)
        if label is None:
            continue
        state = SMState.from_label(label)
        if state is None:
            continue
        # Use updatedAt as the activity proxy. gh CLI returns
        # ``updatedAt`` (camelCase) on issue payloads.
        last_activity_raw = (
            issue.get("updatedAt")
            or issue.get("updated_at")
            or issue.get("createdAt")
            or issue.get("created_at")
        )
        if not last_activity_raw:
            continue
        try:
            last_activity = _dt.datetime.fromisoformat(
                str(last_activity_raw).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        triples.append((number, state, last_activity))

    try:
        graces = _v3_grace_pass(
            issues=triples,
            ledger=ledger,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — Phase 3 defensive guard
        log(f"[sm-v3] grace pass raised: {type(exc).__name__}: {exc}")
        return

    for number, transition in graces:
        prior = transition.metadata.get("prior_state", "unknown")
        audit_body = transition.metadata.get("audit_body", "")
        log(
            f"[sm-v3] grace-block #{number}: prior={prior} → sm:blocked "
            f"(SM_REQUIRE_CONTINUE one-time enforcement)"
        )
        if dry_run:
            continue
        # Apply the label swap + audit comment via the existing v1
        # transport callables. Failures here are logged but not
        # re-raised — the v3 grace pass is additive over the main
        # loop and must not abort it.
        try:
            edit_labels(
                repo,
                number,
                add=[SMState.BLOCKED.value],
                remove=[prior] if prior != "unknown" else [],
            )
        except Exception as exc:  # noqa: BLE001
            log(
                f"[sm-v3] grace-block #{number}: edit_labels failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if audit_body:
            try:
                post_comment(repo, number, audit_body)
            except Exception as exc:  # noqa: BLE001
                log(
                    f"[sm-v3] grace-block #{number}: post_comment failed: "
                    f"{type(exc).__name__}: {exc}"
                )


def run(
    *,
    repo: str = DEFAULT_REPO,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path | None = None,
    v3_authoritative_states: frozenset[str] = frozenset(),
    v3_log_dir: pathlib.Path | None = None,
    # Pre-Phase-4 names — kept as aliases for callers that haven't
    # updated to the new flag names yet. ``v3_dry_run_states`` ->
    # ``v3_authoritative_states``; ``v3_dry_run_log_dir`` ->
    # ``v3_log_dir``. Deprecated; remove when the legacy/ package is
    # deleted (one-month grace per Phase 4 design).
    v3_dry_run_states: frozenset[str] | None = None,
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
    validate_issue: Callable[..., tuple[bool, str]] | None = None,
) -> tuple[int, RunReport]:
    """Run one dispatcher pass. Returns ``(exit_code, report)``.

    Exit codes:
      0  poll completed (zero or more comments posted; state saved)
      1  ``gh`` failed in a way we can't recover from this pass —
         auth, rate limit, transport error. State NOT written;
         s6 supervisor will retry on the next cadence.
    """
    # Phase 4 (#301): merge legacy v3-dry-run kwargs into the new
    # authoritative kwargs. Callers that haven't migrated yet still
    # work; once the legacy/ package is deleted these aliases go
    # with it.
    if v3_dry_run_states is not None and not v3_authoritative_states:
        v3_authoritative_states = v3_dry_run_states
    if v3_dry_run_log_dir is not None and v3_log_dir is None:
        v3_log_dir = v3_dry_run_log_dir

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
                issue: dict[str, Any],
                art_label: str,
                repo: str,
                *,
                design_note_path: pathlib.Path | None = None,
            ) -> str | None:
                # Issue #327 — pass the resolved design path through to
                # the speaking-agent shim so its prompt frontmatter
                # carries a real ``design_note:`` instead of ``(unset)``.
                return spawn_speaking_agent(
                    issue,
                    art_label,
                    repo,
                    design_note_path=design_note_path,
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

    # SM v3 Phase 2/4: stable per-cadence id originally used to pair
    # v1-actual and v3-predicted log entries during the dual-run
    # comparison. Phase 4 renamed the v3 lane to ``v3-actual`` (the
    # handler now applies its own result); the cycle_id is still
    # written so downstream readers of the v1-actual + v3-actual
    # streams can correlate entries during the one-month grace
    # period. Generated once per run() call.
    _cycle_id = _generate_cycle_id(now_iso())

    # SM v3 Phase 3: continue-verb enforcement is gated by the
    # ``SM_REQUIRE_CONTINUE`` env var. Default OFF in the Phase 3 PR.
    # When ON, the dispatcher runs a one-time grace pass per issue
    # (idempotent via the ledger) that transitions in-flight issues
    # to ``sm:blocked`` if their last activity precedes the TTL.
    # Strike-3 escalations are detected per-cycle inside the v3
    # handlers' dual-run shim once Phase 4 retires v1.
    if _v3_enforcement_enabled():
        _apply_v3_grace_pass(
            issues=issues,
            repo=repo,
            ledger=ledger,
            edit_labels=edit_labels,
            post_comment=post_comment,
            dry_run=dry_run,
            now_iso=now_iso,
            log=log,
        )

    fatal_exit = False
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            log(f"[sm-dispatcher] skipping issue with non-integer number: {number!r}")
            continue

        # EC-7 (issue #297): skip issues the operator has explicitly
        # deferred. Deferred state is written by Speaking/Thinking via
        # ``gh_state_mirror.write_deferred``; the mirror's cleanup loop
        # preserves these notes unconditionally (see
        # [[2026-05-19-stale-cycle-dispatcher-gap]]). Without this
        # guard the dispatcher re-surfaces deferred issues every poll
        # cycle because no handler reads the deferred flag.
        #
        # The audit comment is throttled to once per 24h via the v3
        # ``EmittedLedger`` so we don't spam an issue that's parked
        # for days. The lazy import matches the pattern used by other
        # handler-adjacent imports in this loop (avoids module-load
        # circular-import risk).
        from alice_forge.gh_state_mirror import (
            read_state as _read_gh_state,
        )
        _gh = _read_gh_state(repo, number)
        if _gh and _gh.get("type") == "deferred":
            _reason = _gh.get("reason", "no reason given")
            _deferred_by = _gh.get("deferred_by", "unknown")
            _deferred_at = _gh.get("deferred_at", "unknown")
            import datetime as _dt_def
            try:
                _now_dt = _dt_def.datetime.fromisoformat(now_iso())
            except ValueError:
                _now_dt = _dt_def.datetime.now(_dt_def.timezone.utc)
            if not ledger.is_emitted_active(
                number, "deferred-skip", _now_dt
            ):
                _body = (
                    f'[SM] deferred-skip reason="{_reason}" '
                    f"deferred_by={_deferred_by} "
                    f"deferred_at={_deferred_at}"
                )
                if not dry_run:
                    try:
                        post_comment(repo, number, _body)
                    except Exception as exc:  # noqa: BLE001
                        log(
                            f"[sm-dispatcher] #{number}: "
                            f"deferred-skip comment failed: {exc}"
                        )
                ledger.mark_emitted(
                    issue_number=number,
                    side_effect="deferred-skip",
                    emitted_at=_now_dt,
                    ttl_seconds=86400,
                    metadata={
                        "reason": _reason,
                        "deferred_by": _deferred_by,
                        "deferred_at": _deferred_at,
                    },
                )
                log(f"[sm-dispatcher] #{number}: deferred — {_reason}")
            report.skipped_dedup += 1
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
                v3_transitioned = False
                if ACTIVE_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.selected import handle as _h_selected
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_selected,
                        state_for_log=_SMState.SELECTED,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        find_linked_pr=find_linked_pr,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    # v3 owns the transition; legacy v1 handler is
                    # skipped this cadence to avoid double-emitting
                    # the label edit and audit comment.
                    continue
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
                v3_transitioned = False
                if REVIEWING_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.reviewing import handle as _h_reviewing
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_reviewing,
                        state_for_log=_SMState.REVIEWING,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        find_linked_pr=find_linked_pr,
                        pr_merge_status=pr_merge_status,
                        master_ci_status=master_ci_status,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
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
                # SM v3 Phase 4: v3 owns the needs_study transitions when
                # the state is in ``v3_authoritative_states``. Otherwise
                # v1's ``_process_needs_study`` keeps running unchanged.
                v3_transitioned = False
                if NEEDS_STUDY_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.needs_study import handle as _h_ns
                    from alice_forge.sm.states import SMState as _SMState

                    def _v3_resolve(n: int) -> str | None:
                        try:
                            note = _find_resolving_research_note(n, research_dir)
                        except Exception:
                            return None
                        return note.stem if note is not None else None

                    v3_transitioned = _v3_run(
                        handler=_h_ns,
                        state_for_log=_SMState.NEEDS_STUDY,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        research_resolver=_v3_resolve,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
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
                # EC-2 (issue #294) — auto-classify ``art:*`` on draft
                # entry. If the issue lacks any ``art:*`` label, the
                # in-process keyword classifier proposes one (or falls
                # back to ``art:pending``); we apply it via the
                # ``edit_labels`` transport and patch the in-memory
                # issue dict so v3 / legacy / trust filter all see the
                # new label without a round-trip GH fetch. Wrapping in
                # try/except so a transient GH error never aborts the
                # whole cadence — the next pass will retry.
                from alice_forge.dispatcher.art_classifier import (
                    auto_label as _auto_label_art,
                )

                _labels_now = _label_names(issue)
                if not any(lab.startswith("art:") for lab in _labels_now):
                    _suggested = _auto_label_art(
                        title=issue.get("title") or "",
                        body=issue.get("body") or "",
                        existing_labels=_labels_now,
                    )
                    if _suggested:
                        if dry_run:
                            log(
                                f"[sm-dispatcher] DRY-RUN would apply "
                                f"{_suggested!r} to draft #{number} "
                                f"(art-classifier)"
                            )
                        else:
                            try:
                                edit_labels(
                                    repo,
                                    number,
                                    add=[_suggested],
                                    remove=[],
                                )
                            except Exception as _exc:  # noqa: BLE001
                                log(
                                    f"[sm-dispatcher] draft #{number}: "
                                    f"art-classifier failed to apply "
                                    f"{_suggested!r}: "
                                    f"{type(_exc).__name__}: {_exc}"
                                )
                            else:
                                log(
                                    f"[art-classifier] #{number}: "
                                    f"applied {_suggested!r}"
                                )
                                # Patch in-memory issue dict so
                                # downstream v3 + legacy + trust filter
                                # see the new label this pass.
                                issue.setdefault("labels", []).append(
                                    {"name": _suggested}
                                )

                # SM v3 Phase 4: v3 owns sm:draft transitions when the
                # state is in ``v3_authoritative_states``. The legacy
                # ``_process_draft`` still handles the triage-surface
                # write path when v3 returns a non-transition (or no
                # action), so the watcher/surface contract is unchanged
                # until those side-effects are ported.
                v3_transitioned = False
                if DRAFT_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.draft import handle as _h_draft
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_draft,
                        state_for_log=_SMState.DRAFT,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
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
                    validate_issue=validate_issue,
                )
            elif sm_label == DESIGNING_SM_LABEL:
                v3_transitioned = False
                if DESIGNING_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.designing import handle as _h_designing
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_designing,
                        state_for_log=_SMState.DESIGNING,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
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
                v3_transitioned = False
                if DESIGN_REVIEW_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.design_review import handle as _h_dr
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_dr,
                        state_for_log=_SMState.DESIGN_REVIEW,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
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
                v3_transitioned = False
                if DESIGNED_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.designed import handle as _h_designed
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_designed,
                        state_for_log=_SMState.DESIGNED,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
                _process_designed(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    live_spawn_dir=live_spawn_dir,
                    has_live_speaking_spawn=has_live_speaking_spawn,
                    count_running_speaking=count_running_speaking,
                    spawn_speaking=spawn_speaking,
                    max_concurrent_speaking_spawns=max_concurrent_speaking_spawns,
                    dry_run=dry_run,
                    log=log,
                )
            elif sm_label == COMPACTING_SM_LABEL:
                # SM v3 Phase 4: v3 owns sm:compacting transitions.
                v3_transitioned = False
                if COMPACTING_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.compacting import handle as _h_compacting
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_compacting,
                        state_for_log=_SMState.COMPACTING,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
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
                # SM v3 Phase 4: v3 owns sm:building transitions.
                v3_transitioned = False
                if BUILDING_SM_LABEL in v3_authoritative_states:
                    from alice_forge.sm.handlers.building import handle as _h_building
                    from alice_forge.sm.states import SMState as _SMState
                    v3_transitioned = _v3_run(
                        handler=_h_building,
                        state_for_log=_SMState.BUILDING,
                        issue=issue,
                        repo=repo,
                        cycle_id=_cycle_id,
                        ledger=ledger,
                        post_comment=post_comment,
                        edit_labels=edit_labels,
                        close_issue=close_issue,
                        list_comments=list_comments,
                        find_linked_pr=find_linked_pr,
                        trusted_authors=trusted_authors,
                        log_dir=v3_log_dir,
                        dry_run=dry_run,
                        now_iso=now_iso,
                        log=log,
                    )
                if v3_transitioned:
                    continue
                _process_building(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    has_live_speaking_spawn=has_live_speaking_spawn,
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


def _v3_authoritative_states_from_env() -> frozenset[str]:
    """The full set of v3-authoritative states, with an env-var kill switch.

    Phase 4 completion (#313 wired the code; this wires the
    enablement): every non-terminal SMState is authoritative under v3
    by default. v3 handlers own transition decisions and v1 legacy
    only runs as a fallback for side-effects v3 hasn't ported (spawn
    dispatch, hello, dep-check, rebase, post-merge cleanup, verify).

    Emergency kill switch: set ``SM_V3_DISABLE`` (any truthy value:
    ``1``, ``true``, ``yes``, ``on``) to return an empty set, which
    makes every ``if X_SM_LABEL in v3_authoritative_states:`` check
    False and falls back to pure-v1 behavior. Useful if a v3 handler
    bug surfaces in production and a fix needs more than one cadence
    to land. Setting + reloading the s6 service env restores pre-#313
    behavior without redeploying code.
    """
    import os as _os

    from alice_forge.sm.states import SMState

    if _os.environ.get("SM_V3_DISABLE", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        return frozenset()
    return frozenset(s.value for s in SMState)


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
                v3_authoritative_states=_v3_authoritative_states_from_env(),
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
