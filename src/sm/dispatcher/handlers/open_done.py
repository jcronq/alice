"""Issue #174 sweep handler — OPEN issues at ``sm:done`` are research-note close-stragglers. Enforces the ``[SM] exit-transition`` gate (or worker's self-transition fallback, #195) and closes the issue.
"""

from __future__ import annotations

from sm.dispatcher.handlers._common import *  # noqa: F401, F403


def _process_open_done(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    close_issue: CloseIssueFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """Close OPEN issues at ``sm:done`` once their exit gate is satisfied (issue #174).

    The ``art:research_note`` worker flips ``sm:selected → sm:done``
    directly without producing a PR, so the canonical close path
    (:func:`_process_reviewing` → merged PR → ``gh issue close``) never
    fires for these tasks. Without this handler the issue stays in the
    open list forever and the work looks "stuck" from the viewer's
    lens even though the vault note exists.

    Behaviour for ``art:research_note`` issues:

      * If a trusted close-signal comment is present (see
        :func:`_research_close_signal` — either ``[SM] exit-transition=
        <value>`` or the worker's own ``[SM] transition from=selected
        to=done`` audit comment) → close the issue and emit a
        ``[SM] transition from=done to=done reason=...`` audit comment
        recording the close. Clears the ``exit_required_posted`` ledger
        entry.
      * If missing → post the ``[SM] exit-transition-required`` reminder
        once (deduped via the state ledger + a defensive comment scan
        so a state-file reset doesn't re-spam) and stay.

    The two-signal gate (#195 follow-up to #174): the original #174
    design required the explicit ``[SM] exit-transition=<value>`` verb,
    but no producer in this codebase emits it — workers post the
    ``[SM] transition from=selected to=done`` audit comment per the
    ``(sm:selected, art:research_note)`` dispatch row. Without the
    fallback, the close path was dead-on-arrival and every research-note
    completion required ``gh issue close`` by hand (#105, #178, #179,
    #180 on 2026-05-13).

    For any other artifact (``art:code`` / ``art:config_change`` /
    ``art:experiment``) an OPEN-at-``sm:done`` issue is a state-machine
    aberration — the close should have happened on the
    ``sm:reviewing → sm:done`` transition. Log the surprise and skip;
    a human picks it up. We do NOT auto-close art:code without the
    PR-merged + CI-green pedigree the canonical path enforces.
    """
    number = issue["number"]
    names = _label_names(issue)
    art_labels = [n for n in names if n in ART_LABEL_WHITELIST]
    if not art_labels:
        log(
            f"[sm-dispatcher] open-done skip #{number}: no whitelisted art:* label "
            f"({names!r})"
        )
        return
    art_label = sorted(art_labels)[0]

    if art_label != "art:research_note":
        log(
            f"[sm-dispatcher] open-done skip #{number}: OPEN at {DONE_SM_LABEL} with "
            f"{art_label} — expected the canonical sm:reviewing → sm:done path "
            f"to have closed this; leaving for human review"
        )
        return

    # art:research_note — gate on a trusted close-signal comment. Two
    # shapes are accepted (see :func:`_research_close_signal`):
    #
    #   1. ``[SM] exit-transition=<value>`` — explicit, preferred,
    #      carries disseminate/spawn-code/both metadata. Issue #174.
    #   2. ``[SM] transition from=selected to=done reason=...`` — the
    #      worker's own audit comment. Per #195, this is the only signal
    #      any producer in this codebase actually emits, so the close
    #      path closes on it; otherwise the migration story is "manual
    #      close forever" and that defeats the auto-sweep.
    try:
        has_signal, signal_reason = _research_close_signal(
            repo, number, list_comments, trusted_authors, log
        )
    except GHCommandError:
        # Fatal gh error (auth / rate limit) — re-raised by helper.
        raise

    if has_signal:
        suffix = signal_reason or "exit-transition recorded"
        reason = f"art:research_note + {suffix}"
        body = render_transition_comment(DONE_SM_LABEL, DONE_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would close #{number}: "
                f"art:research_note + {suffix}"
            )
            report.research_closed += 1
            return
        try:
            close_issue(repo, number)
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] open-done failed to close #{number}: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_exit_required(number)
        report.research_closed += 1
        log(
            f"[sm-dispatcher] open-done closed #{number}: "
            f"art:research_note + {suffix}"
        )
        return

    # No exit-transition yet — post the reminder once.
    if state.has_exit_required(number):
        log(
            f"[sm-dispatcher] open-done #{number}: still waiting on exit-transition "
            f"(reminder already posted)"
        )
        return

    # Defensive comment-prefix scan: catches the state-file-reset case
    # where the ledger entry was lost but the reminder is already on
    # the issue. Without this, a wiped state file would re-spam the
    # comment on every open research_note + done issue.
    try:
        existing = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to scan comments for #{number} before "
            f"posting exit-transition-required: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    for item in existing:
        body_text = item.get("body")
        author = item.get("author")
        if isinstance(author, dict):
            login = author.get("login")
        elif isinstance(author, str):
            login = author
        else:
            login = None
        if (
            isinstance(body_text, str)
            and body_text.startswith(EXIT_TRANSITION_REQUIRED_PREFIX)
            and login in trusted_authors
        ):
            # Adopt the on-issue evidence as the dedup signal even
            # though our local ledger was empty.
            state.mark_exit_required(number)
            log(
                f"[sm-dispatcher] open-done #{number}: exit-transition-required "
                f"already on issue (ledger reset); marking and skipping"
            )
            return

    reminder = render_exit_transition_required_comment(number, timestamp=now_iso())
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would post exit-transition-required on "
            f"#{number}"
        )
        report.exit_required_posted += 1
        return
    try:
        post_comment(repo, number, reminder)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] open-done failed to post exit-transition-required "
            f"on #{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    state.mark_exit_required(number)
    report.exit_required_posted += 1
    log(
        f"[sm-dispatcher] open-done #{number}: posted exit-transition-required "
        f"(art:research_note + {DONE_SM_LABEL} + OPEN)"
    )
