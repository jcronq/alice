"""Handler for ``sm:selected`` issues — return-to-study, dependency gating, hello + audit, T1 transition to ``sm:reviewing``, and Phase 2 spawn dispatch.
"""

from __future__ import annotations

from sm.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_selected(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    has_live_spawn: Callable[[int], bool] | None,
    count_running: Callable[[], int] | None,
    spawn: Callable[[dict[str, Any], str, str], str | None] | None,
    max_concurrent_spawns: int,
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
    get_issue: Callable[[int], dict[str, Any] | None] | None = None,
    has_live_thinking_spawn: Callable[[int], bool] | None = None,
    count_running_thinking: Callable[[], int] | None = None,
    spawn_thinking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    max_concurrent_thinking_spawns: int = MAX_CONCURRENT_THINKING_SPAWNS,
) -> None:
    """Return-to-study check + Hello + T1 (selected → reviewing) + Phase 2
    spawn for one sm:selected issue.

    Order matters: trust filter → return-to-study scan (terminating: an
    explicit ``[SM] return-to-study`` from the worker reverses the
    state before any new work fires) → dependency check (issue #176:
    rejected dep → ``sm:blocked``, terminating) → hello (idempotent) →
    T1 if linked PR exists (terminating, since work is already in
    flight) → otherwise Phase 2 spawn (gated by concurrency cap + dedup
    on a live spawn dir + open hard-deps from issue #176).

    ``get_issue`` (issue #176) is the per-issue lookup used to resolve
    ``Depends on #N`` references on the body. ``None`` disables the
    dependency gate entirely — production callers always bind it; tests
    that don't exercise the gate can leave it unset.

    Spawn dispatch (sub-issue 7 / #186): the
    :data:`SPAWN_MAP` row's ``persona`` field selects which spawn
    machinery to invoke. ``persona == "thinking"`` (the SM v2 design
    lane for ``art:code``) routes to ``spawn_thinking`` and gates
    against the thinking-lane's dedup / concurrency helpers
    (``has_live_thinking_spawn`` / ``count_running_thinking`` /
    :data:`MAX_CONCURRENT_THINKING_SPAWNS`). All other personae
    (``"worker"`` for ``art:config_change`` / ``art:research_note`` /
    ``art:experiment``) route to the v1 ``spawn`` callable, same as
    the pre-cutover behavior.
    """
    number = issue["number"]
    decision = evaluate_trust(issue, trusted_authors=trusted_authors)
    if not decision.accepted:
        log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
        report.skipped_trust += 1
        return

    # ----- return-to-study check -----
    # A worker that realises it can't advance from sm:selected without
    # further thinking input emits ``[SM] return-to-study reason=...``;
    # the dispatcher reverses the state on the next pass. This must
    # short-circuit the hello/T1/spawn flow — once the issue is going
    # back to needs_study there's no point posting a hello or queuing a
    # new spawn.
    try:
        sel_comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] selected #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        sel_comments = []
    from sm.comments import ReturnToStudy
    parsed_return = _find_parsed_comment_of_type(
        sel_comments,
        ReturnToStudy,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed_return is not None:
        reason = f'return-to-study reason="{parsed_return.reason}"'
        transition_body = render_transition_comment(
            ACTIVE_SM_LABEL, NEEDS_STUDY_SM_LABEL, reason
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"selected → needs_study ({reason})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, ACTIVE_SM_LABEL, NEEDS_STUDY_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[NEEDS_STUDY_SM_LABEL],
                remove=[ACTIVE_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] selected #{number}: "
                f"failed return-to-study transition: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append(
            (number, ACTIVE_SM_LABEL, NEEDS_STUDY_SM_LABEL)
        )
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"selected → needs_study ({reason})"
        )
        return

    # ----- dependency parse + resolve (issue #176) -----
    # ``Depends on #N`` / ``Blocked by #N`` / etc. live in plain prose
    # on the issue body and any trusted-author amendment comments. The
    # parser is anchored to start-of-line so prose inside ordinary
    # comments doesn't produce false positives.
    from sm.comments import parse_dependencies as _parse_deps

    dep_sources: list[str] = []
    body_text = issue.get("body")
    if isinstance(body_text, str) and body_text:
        dep_sources.append(body_text)
    for c in sel_comments:
        if not isinstance(c, dict):
            continue
        cb = c.get("body")
        if not isinstance(cb, str) or not cb:
            continue
        # Skip ``[SM] ...`` audit/protocol comments — those are the
        # dispatcher's own log lines and won't contain user-authored
        # dependency directives. The trust filter further restricts to
        # trusted authors so a drive-by commenter can't inject deps
        # that would gate or transition the issue.
        if cb.startswith("[SM] "):
            continue
        author = _comment_author_login(c)
        if author not in trusted_authors:
            continue
        dep_sources.append(cb)
    parsed_deps = _parse_deps("\n".join(dep_sources)) if dep_sources else None

    blocking_deps: tuple[int, ...] = ()
    if parsed_deps is not None and (parsed_deps.hard or parsed_deps.soft):
        if get_issue is None:
            # Production wires get_issue via ``run()``; tests that don't
            # exercise the gate leave it None. Treat as "no resolver" =
            # don't block, but log so the operator notices if it ever
            # fires in prod.
            log(
                f"[sm-dispatcher] #{number}: deps "
                f"hard={list(parsed_deps.hard)} soft={list(parsed_deps.soft)} "
                f"present but no get_issue resolver bound — "
                f"skipping dependency gate"
            )
        else:
            resolution = resolve_dependencies(
                parsed_deps.hard, get_issue, log=log
            )
            if resolution.rejected:
                rejected_str = ", ".join(f"#{n}" for n in resolution.rejected)
                inner_reason = (
                    f"dependency {rejected_str} was rejected"
                )
                transition_body = (
                    f'[SM] transition from=selected to=blocked '
                    f'reason="{inner_reason}" '
                    f'unblocked_by="speaking to re-scope"'
                )
                if dry_run:
                    log(
                        f"[sm-dispatcher] DRY-RUN would transition "
                        f"#{number}: selected → blocked ({inner_reason})"
                    )
                    report.transitioned += 1
                    report.transitions.append(
                        (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
                    )
                    return
                try:
                    edit_labels(
                        repo,
                        number,
                        add=[BLOCKED_SM_LABEL],
                        remove=[ACTIVE_SM_LABEL],
                    )
                    post_comment(repo, number, transition_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] selected #{number}: "
                        f"failed dependency-rejected transition: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    return
                report.transitioned += 1
                report.transitions.append(
                    (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
                )
                log(
                    f"[sm-dispatcher] transitioned #{number}: "
                    f"selected → blocked ({inner_reason})"
                )
                return
            # Soft-dep + missing branches are log-only; the hard-blocking
            # gate is applied below, after hello + T1, so the audit comment
            # still posts even when the issue is queued.
            blocking_deps = resolution.blocking

    art_label = decision.art_label or "art:unknown"

    # Hello (dedup-guarded)
    if state.has_hello(number):
        report.skipped_dedup += 1
    else:
        body = render_hello_comment(number, art_label, timestamp=now_iso())
        if dry_run:
            log(f"[sm-dispatcher] DRY-RUN would post on #{number}: {body}")
            report.posted += 1
            report.posted_numbers.append(number)
        else:
            try:
                post_comment(repo, number, body)
            except GHCommandError as exc:
                log(f"[sm-dispatcher] failed to comment on #{number}: {exc}")
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            state.mark_hello(number)
            report.posted += 1
            report.posted_numbers.append(number)
            log(f"[sm-dispatcher] posted dispatcher-hello on #{number}")

    # T1: sm:selected → sm:reviewing if a linked open PR exists.
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to look up PR for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is not None:
        # T1 fires only when the linked PR is still OPEN.
        # ``gh_find_linked_pr`` queries ``--state all`` (so the T2/T3
        # path can find merged PRs); we filter here so an sm:selected
        # issue whose PR has already merged or closed doesn't get
        # bounced to sm:reviewing — that lifecycle stage is past.
        pr_state = (pr.get("state") or "").upper()
        if pr_state != "OPEN":
            log(
                f"[sm-dispatcher] #{number} selected but linked PR is "
                f"{pr_state!r} (not OPEN) — not transitioning to reviewing"
            )
            return
        pr_url = pr.get("url") or "<unknown>"
        transition_body = render_transition_comment(
            ACTIVE_SM_LABEL, REVIEWING_SM_LABEL, f"PR opened: {pr_url}"
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"selected → reviewing ({pr_url})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, ACTIVE_SM_LABEL, REVIEWING_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[REVIEWING_SM_LABEL],
                remove=[ACTIVE_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed to transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append((number, ACTIVE_SM_LABEL, REVIEWING_SM_LABEL))
        log(f"[sm-dispatcher] transitioned #{number}: selected → reviewing")
        return

    # No linked PR yet — Phase 2 spawn path.
    spawn_config = _current_spawn_map().get((ACTIVE_SM_LABEL, art_label))
    if spawn_config is None:
        log(
            f"[sm-dispatcher] spawn skip #{number}: "
            f"unrecognized artifact {art_label!r}"
        )
        return

    persona = spawn_config.get("persona", "worker")

    # Persona selects the spawn lane (sub-issue 7 / #186). The thinking
    # lane uses its own dedup + concurrency helpers so a long-running
    # design loop can't starve the v1 worker pool (and vice versa).
    if persona == "thinking":
        lane_spawn = spawn_thinking
        lane_has_live = has_live_thinking_spawn
        lane_count_running = count_running_thinking
        lane_cap = max_concurrent_thinking_spawns
        lane_label = "thinking"
    else:
        lane_spawn = spawn
        lane_has_live = has_live_spawn
        lane_count_running = count_running
        lane_cap = max_concurrent_spawns
        lane_label = "worker"

    # Caller passes the lane's helpers as None to disable spawning
    # entirely (tests that only care about hello/T1 paths take this
    # escape hatch).
    if lane_spawn is None or lane_count_running is None or lane_has_live is None:
        return

    # Issue #176 — gate the spawn on any unresolved hard dependency.
    # No spawn-started comment, no label change; the issue stays at
    # sm:selected and the dispatcher re-checks on the next pass when
    # the dep may have closed. Logged once per pass per blocking dep
    # so the operator can see what's holding the queue.
    if blocking_deps:
        blocked_str = ", ".join(f"#{n}" for n in blocking_deps)
        log(
            f"[sm-dispatcher] spawn skip #{number}: "
            f"blocked by {blocked_str}"
        )
        report.spawn_skipped_blocked_deps += 1
        return

    # Dedup on a live spawn dir (issue #115). The historic
    # [SM] spawn-started audit comment is NOT consulted — if the
    # worker died after posting the comment but before opening a PR,
    # we want the next pass to retry, not be permanently gated by the
    # comment. The lane-scoped helper also reaps stale ``spawn-<N>-*``
    # dirs into ``.finished/`` so they don't keep getting re-checked.
    if lane_has_live(number):
        log(
            f"[sm-dispatcher] spawn skip #{number}: live {lane_label} "
            f"spawn dir already running"
        )
        return

    # Issue #202 — silent thinking-spawn guard. The thinking lane has
    # no equivalent of the worker lane's "open a PR" terminal signal at
    # sm:selected; instead, the thinking-agent is expected to post
    # ``[SM] design-ready`` once the design note is written. If a prior
    # spawn already fired (audit comment present) but no design-ready
    # ever followed AND no live spawn dir remains, the shim completed
    # without doing anything useful — retrying just loops forever (the
    # observed failure mode on #194: ~1125 respawns over 22h). Block
    # the issue rather than re-spawning; an operator (or sub-issue 3
    # shim replacement) can unblock once the underlying entrypoint is
    # wired up. Scoped to ``persona == "thinking"`` so the v1 worker
    # retry semantics above stay untouched.
    if persona == "thinking":
        saw_thinking_spawn_started = False
        saw_design_ready = False
        for c in sel_comments:
            if not isinstance(c, dict):
                continue
            body = c.get("body")
            if not isinstance(body, str):
                continue
            login = _comment_author_login(c)
            if not isinstance(login, str) or login not in trusted_authors:
                continue
            if body.startswith(THINKING_SPAWN_STARTED_PREFIX):
                saw_thinking_spawn_started = True
            elif body.startswith("[SM] design-ready"):
                # Matches both the agent-emitted ``[SM] design-ready``
                # and the dispatcher's ``[SM] design-ready-audit`` echo;
                # either is evidence that the design phase produced its
                # terminal signal.
                saw_design_ready = True
        if saw_thinking_spawn_started and not saw_design_ready:
            reason = (
                "thinking-agent spawn exited without posting "
                "[SM] design-ready (see #202)"
            )
            transition_body = render_transition_comment(
                ACTIVE_SM_LABEL, BLOCKED_SM_LABEL, reason
            )
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                    f"selected → blocked ({reason})"
                )
                report.transitioned += 1
                report.transitions.append(
                    (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
                )
                return
            try:
                edit_labels(
                    repo,
                    number,
                    add=[BLOCKED_SM_LABEL],
                    remove=[ACTIVE_SM_LABEL],
                )
                post_comment(repo, number, transition_body)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] selected #{number}: "
                    f"failed silent-spawn-failure transition: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            report.transitioned += 1
            report.transitions.append(
                (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
            )
            log(
                f"[sm-dispatcher] transitioned #{number}: "
                f"selected → blocked ({reason})"
            )
            return

    live = lane_count_running()
    if live >= lane_cap:
        log(
            f"[sm-dispatcher] spawn skip #{number}: {lane_label} "
            f"concurrency cap reached ({live}/{lane_cap}) — queued for "
            f"next pass"
        )
        return

    if dry_run:
        if persona == "thinking":
            preview = compose_thinking_spawn_prompt(issue)[:240]
        else:
            preview = compose_spawn_prompt(issue, spawn_config)[:240]
        log(
            f"[sm-dispatcher] DRY-RUN would spawn {lane_label} on "
            f"#{number} art={art_label} "
            f"(running={live}/{lane_cap})"
        )
        log(f"[sm-dispatcher] DRY-RUN prompt preview: {preview!r}")
        report.spawned += 1
        report.spawn_records.append((number, art_label, "<dry-run>"))
        return

    try:
        spawn_id = lane_spawn(issue, art_label, repo)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to spawn on #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        log(f"[sm-dispatcher] spawn OS error on #{number}: {exc}")
        return
    if spawn_id is None:
        return
    report.spawned += 1
    report.spawn_records.append((number, art_label, spawn_id))
