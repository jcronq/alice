"""Handler for ``sm:needs_study`` issues.

Writes the hint file into ``inner/notes/`` for the thinking agent, posts the ``[SM] study-hint-written`` audit comment, and watches for either (a) a trusted ``[SM] study-complete`` comment or (b) a research note in the vault whose frontmatter resolves the issue (#212).
"""

from __future__ import annotations

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403


def _process_needs_study(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    notes_dir: pathlib.Path,
    research_dir: pathlib.Path,
    trusted_authors: frozenset[str],
    art_whitelist: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """Hint emission + comment-driven transitions for one ``sm:needs_study`` issue.

    Three-phase pass:

      1. **Hint emission.** Idempotent on the ledger field
         ``DispatcherState.needs_study_hinted`` and defensively on the
         ``[SM] study-hint-written`` audit comment from a trusted
         author. On first encounter we write
         ``inner/notes/sm-needs-study-issue<N>.md`` (issue body +
         frontmatter the thinking-agent's wake prompt picks up — see
         #6) and post the audit comment.

      2. **Comment-driven transitions.** Scan comments newest-first
         via :func:`alice_forge.comments.parse_comment`. The first parsed
         study-verb wins:

           * ``study-complete`` → ``sm:selected``, swap ``art:*`` if
             the parsed art label differs from the issue's current one
             (the parser already validated whitelist membership).
           * ``study-blocked``  → ``sm:blocked``.
           * ``study-rejected`` → ``sm:rejected``.
           * ``study-progress`` → no-op (thinking still working);
             ``study-progress`` resets the 7-day stall clock in #4.

         Comments that aren't ``[SM] study-*`` (audit comments,
         human prose) are ignored. The trust check inside each parser
         keeps a random commenter from forging a transition.

      3. **Vault auto-advance (issue #212).** If step 2 finds no
         parsed study-verb yet, scan ``research_dir`` for a note whose
         frontmatter contains ``resolves_issue: <N>`` (scalar) or
         ``resolves_issues: [<N>, ...]`` (flow list). On match the
         dispatcher posts a synthetic
         ``[SM] study-complete art=art:research_note
         findings=[[<note-slug>]] auto-posted=true`` audit comment and
         returns; the next pass picks the comment up via step 2 and
         the issue transitions out of ``sm:needs_study`` naturally.

         Rationale: thinking writes the groomed research note but
         frequently forgets to post the audit comment, leaving the
         issue parked indefinitely (cf. #198/#200/#201 on
         2026-05-14). The mechanics belong in deterministic dispatcher
         code, not the agent's prompt — see the feedback note
         ``procedural-logic-in-code``.

         Idempotency: once the synthetic comment is on the issue, the
         next pass parses it as a real ``study-complete`` (parsers
         tolerate the trailing ``auto-posted=true`` field) and step 2
         transitions normally. Step 3 doesn't re-fire because step 2
         no longer returns ``parsed_study is None``.
    """
    number = issue["number"]

    # ----- step 1: hint emission -----
    # The comments list is needed for both the audit-comment dedup
    # check and the transition scan below, so fetch once and reuse.
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] needs_study #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    if state.has_needs_study_hint(number):
        already_hinted = True
    elif _has_prior_study_hint_audit(comments, trusted_authors=trusted_authors):
        # Defensive: state file lost, audit comment persists. Mark in
        # the ledger so the next pass takes the fast path.
        state.mark_needs_study_hint(number)
        already_hinted = True
    else:
        already_hinted = False

    if not already_hinted:
        note_path = notes_dir / f"sm-needs-study-issue{number}.md"
        note_body = render_study_hint_note_body(issue)
        audit_body = render_study_hint_audit_comment(
            number, note_path, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would write hint for #{number} "
                f"at {note_path} and post audit comment"
            )
            report.hinted += 1
        else:
            try:
                notes_dir.mkdir(parents=True, exist_ok=True)
                note_path.write_text(note_body)
            except OSError as exc:
                log(
                    f"[sm-dispatcher] needs_study #{number}: "
                    f"failed to write hint at {note_path}: {exc}"
                )
                return
            try:
                post_comment(repo, number, audit_body)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] needs_study #{number}: "
                    f"failed to post study-hint-written: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                # The hint file is on disk. We didn't mark the ledger,
                # so the next pass will retry the comment post — the
                # audit-comment scan above will see no prior audit and
                # re-attempt (the file write is idempotent on the
                # known filename).
                return
            state.mark_needs_study_hint(number)
            report.hinted += 1
            log(
                f"[sm-dispatcher] needs_study #{number}: hint written "
                f"at {note_path}"
            )

    # ----- step 2: comment-driven transitions -----
    # Local import to avoid a top-of-module cycle: ``alice_forge.comments``
    # imports ``ART_LABEL_WHITELIST`` / ``TRUSTED_AUTHORS`` from this
    # module.
    from alice_forge.comments import (
        StudyBlocked,
        StudyComplete,
        StudyProgress,
        StudyRejected,
        parse_comment,
    )

    parsed_study = None
    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        login = _comment_author_login(c)
        parsed = parse_comment(
            body,
            login,
            trusted_authors=trusted_authors,
            log=log,
        )
        if isinstance(
            parsed, (StudyComplete, StudyBlocked, StudyRejected, StudyProgress)
        ):
            parsed_study = parsed
            break

    if parsed_study is None:
        # Step 3 — vault auto-advance (issue #212). Thinking's research
        # note carries ``resolves_issue: <N>`` in its frontmatter; if
        # we find one matching this issue, synthesize the
        # study-complete audit comment that thinking forgot to post.
        resolving_note = _find_resolving_research_note(number, research_dir)
        if resolving_note is not None:
            slug = resolving_note.stem
            synth_body = render_auto_study_complete_comment(slug)
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would auto-post "
                    f"study-complete for #{number} from "
                    f"{resolving_note} (slug={slug})"
                )
                return
            try:
                post_comment(repo, number, synth_body)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] needs_study #{number}: "
                    f"failed to auto-post study-complete: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            log(
                f"[sm-dispatcher] needs_study #{number}: auto-posted "
                f"study-complete from {resolving_note} (slug={slug}); "
                f"transition fires on next pass"
            )
            # Intentional: the freshly-posted comment isn't in the
            # ``comments`` list we already fetched, so the transition
            # has to wait for the next pass. Returning here keeps the
            # one-action-per-pass invariant the rest of the handler
            # follows.
            return
        log(
            f"[sm-dispatcher] needs_study #{number}: "
            f"no parsed study-* comment yet"
        )
        return

    if isinstance(parsed_study, StudyProgress):
        # Thinking checkpointed but hasn't decided yet. Sub-issue #4
        # will hang the 7-day stall sweep off this branch.
        log(
            f"[sm-dispatcher] needs_study #{number}: thinking still "
            f"working (note=[[{parsed_study.note}]])"
        )
        return

    # Transition verb. Build the (target, reason, add, remove) tuple
    # per verdict, then apply uniformly.
    current_art = _current_art_label(issue, art_whitelist)
    if isinstance(parsed_study, StudyComplete):
        target = ACTIVE_SM_LABEL
        reason = (
            f"study-complete findings=[[{parsed_study.findings}]] "
            f"art={parsed_study.art_label}"
        )
        add_labels = [target]
        remove_labels = [NEEDS_STUDY_SM_LABEL]
        if (
            parsed_study.art_label != current_art
            and current_art is not None
        ):
            add_labels.append(parsed_study.art_label)
            remove_labels.append(current_art)
        elif current_art is None:
            # Issue carried no whitelisted art:* before — apply the
            # parsed one rather than leave the issue art-less.
            add_labels.append(parsed_study.art_label)
    elif isinstance(parsed_study, StudyBlocked):
        target = BLOCKED_SM_LABEL
        reason = f"study-blocked reason=\"{parsed_study.reason}\""
        add_labels = [target]
        remove_labels = [NEEDS_STUDY_SM_LABEL]
    elif isinstance(parsed_study, StudyRejected):
        target = REJECTED_SM_LABEL
        reason = f"study-rejected reason=\"{parsed_study.reason}\""
        add_labels = [target]
        remove_labels = [NEEDS_STUDY_SM_LABEL]
    else:  # pragma: no cover — exhaustively matched above.
        return

    transition_body = render_transition_comment(
        NEEDS_STUDY_SM_LABEL, target, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"needs_study → {target} ({reason})"
        )
        report.transitioned += 1
        report.transitions.append((number, NEEDS_STUDY_SM_LABEL, target))
        return
    try:
        edit_labels(repo, number, add=add_labels, remove=remove_labels)
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] needs_study #{number}: "
            f"failed to transition to {target}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append((number, NEEDS_STUDY_SM_LABEL, target))
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"needs_study → {target} ({reason})"
    )
