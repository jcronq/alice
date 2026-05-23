"""Handler for ``sm:designed`` issues, plus the build-spawn helper.

On entry, drops the ``compact.signal`` into the per-issue thinking spawn dir; on subsequent passes, spawns the speaking-build agent (SM v2 build lane, issue #184). The two-step is intentional — the thinking agent compacts its context before the speaking agent picks up the design.
"""

from __future__ import annotations

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403


_DESIGN_READY_NOTE_RE = re.compile(r"note=\[\[([^\]\s]+)\]\]")


def _resolve_design_note_path(slug: str) -> pathlib.Path | None:
    """Resolve a ``[[<slug>]]`` wikilink to a real filesystem path.

    Issue #327. The thinking-design lane writes its final design output
    to ``cortex-memory/designs/<slug>.md``; the ``[SM] design-ready
    note=[[<slug>]]`` audit comment carries only the slug. The build
    worker needs the absolute path so it can load the approved design.

    Returns the first existing path under :data:`DESIGNS_DIR` then
    :data:`RESEARCH_NOTES_DIR` (pre-cutover designs landed in
    research/). Returns ``None`` if neither location holds the file —
    the caller should surface that as a ``[SM] design-ready-invalid``
    audit rather than silently spawning a doomed build.

    Reads :data:`DESIGNS_DIR` / :data:`RESEARCH_NOTES_DIR` as module
    globals at call time (not as ``def``-time default args), so test
    monkeypatching of the module attribute propagates here.
    """
    candidate = DESIGNS_DIR / f"{slug}.md"
    if candidate.is_file():
        return candidate
    candidate = RESEARCH_NOTES_DIR / f"{slug}.md"
    if candidate.is_file():
        return candidate
    return None


def _find_design_ready_slug(
    comments: list[dict[str, Any]],
    *,
    trusted_authors: frozenset[str],
) -> str | None:
    """Scan ``comments`` newest-first for ``[SM] design-ready note=[[<slug>]]``.

    Returns the slug from the first trusted-author match. Ignores the
    ``[SM] design-ready-audit`` echo (dispatcher self-emission) so the
    resolver always picks the agent's authoritative version. Returns
    ``None`` if no design-ready comment is present from a trusted
    author — the caller falls back to ``design_note_path=None``,
    matching pre-#327 behaviour.
    """
    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        if not body.startswith("[SM] design-ready"):
            continue
        if body.startswith("[SM] design-ready-audit"):
            continue
        login = _comment_author_login(c)
        if not isinstance(login, str) or login not in trusted_authors:
            continue
        m = _DESIGN_READY_NOTE_RE.search(body)
        if m is None:
            continue
        return m.group(1)
    return None


def _process_designed(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    live_spawn_dir: Callable[[int], pathlib.Path | None] | None,
    dry_run: bool,
    log: Callable[[str], None],
    has_live_speaking_spawn: Callable[[int], bool] | None = None,
    count_running_speaking: Callable[[], int] | None = None,
    spawn_speaking: Callable[..., str | None] | None = None,
    max_concurrent_speaking_spawns: int = MAX_CONCURRENT_SPEAKING_SPAWNS,
) -> None:
    """sm:designed → next-phase routing for one issue.

    For ``(sm:designed, art:code)`` (sub-issue 7 / #186): spawn the
    per-issue speaking-agent build lane (:func:`spawn_speaking_agent`),
    then transition the issue ``sm:designed → sm:building`` so
    :func:`_process_building` waits for the speaking-agent's draft PR
    on the next pass.

    For other artifact labels with no ``(sm:designed, *)`` row in
    :data:`SPAWN_MAP`: fall back to the legacy compact-signal behavior
    (locate the live thinking-agent spawn dir, drop a
    ``compact.signal``, transition ``sm:designed → sm:compacting``).
    The compact lane is preserved so an in-flight pre-cutover agent on
    a non-art:code task can finish without the dispatcher stranding it
    at ``sm:designed``.

    Speaking-lane spawn helpers default to ``None`` for tests that
    only exercise the compact-signal path; production wires them in
    :func:`run`.
    """
    number = issue["number"]
    art_label = "art:unknown"
    for name in _label_names(issue):
        if name.startswith("art:") and name in ART_LABEL_WHITELIST:
            art_label = name
            break

    spawn_config = _current_spawn_map().get((DESIGNED_SM_LABEL, art_label))
    persona = spawn_config.get("persona") if spawn_config else None

    if persona == "speaking":
        _designed_spawn_speaking(
            issue=issue,
            repo=repo,
            number=number,
            art_label=art_label,
            report=report,
            post_comment=post_comment,
            edit_labels=edit_labels,
            list_comments=list_comments,
            trusted_authors=trusted_authors,
            has_live_speaking_spawn=has_live_speaking_spawn,
            count_running_speaking=count_running_speaking,
            spawn_speaking=spawn_speaking,
            max_concurrent_speaking_spawns=max_concurrent_speaking_spawns,
            dry_run=dry_run,
            log=log,
        )
        return

    # Legacy compact-signal lane (pre-cutover thinking-agent that
    # restarts itself in build mode). Kept so an in-flight non-art:code
    # issue at sm:designed isn't stranded by the cutover.
    spawn_path: pathlib.Path | None = None
    if live_spawn_dir is not None:
        spawn_path = live_spawn_dir(number)

    if spawn_path is None:
        log(
            f"[sm-dispatcher] designed #{number}: WARNING — no live "
            f"per-issue spawn dir; cannot write compact signal. "
            f"Leaving at sm:designed for the next pass / human triage."
        )
        return

    reason = f"compact signal at {spawn_path / COMPACT_SIGNAL_FILENAME}"
    transition_body = render_transition_comment(
        DESIGNED_SM_LABEL, COMPACTING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"designed → compacting ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNED_SM_LABEL, COMPACTING_SM_LABEL)
        )
        return

    signal_path = spawn_path / COMPACT_SIGNAL_FILENAME
    try:
        signal_path.write_text("compact\n")
    except OSError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: failed to write "
            f"compact signal at {signal_path}: {exc}"
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[COMPACTING_SM_LABEL],
            remove=[DESIGNED_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: "
            f"failed to transition to compacting: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNED_SM_LABEL, COMPACTING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designed → compacting ({reason})"
    )


def _designed_spawn_speaking(
    *,
    issue: dict[str, Any],
    repo: str,
    number: int,
    art_label: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    has_live_speaking_spawn: Callable[[int], bool] | None,
    count_running_speaking: Callable[[], int] | None,
    spawn_speaking: Callable[..., str | None] | None,
    max_concurrent_speaking_spawns: int,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:designed → sm:building: spawn the speaking-agent build lane.

    Sub-issue 7 (#186). Mirrors the spawn block in
    :func:`_process_selected` for the thinking lane: dedup on a live
    speaking-lane spawn dir, gate on the lane's concurrency cap, then
    invoke ``spawn_speaking`` and transition the issue's label to
    ``sm:building`` so the next dispatcher pass picks the draft PR up
    via :func:`_process_building`.

    The transition runs BEFORE the spawn — without it, the next pass
    would re-enter ``_process_designed`` and double-spawn (the live
    spawn dir dedup would only catch this AFTER the first spawn has
    written its pidfile; a slow Popen could allow a race). Posting the
    label change first also matches the pattern in
    ``_process_selected`` for the v1 worker pool.
    """
    if (
        spawn_speaking is None
        or has_live_speaking_spawn is None
        or count_running_speaking is None
    ):
        log(
            f"[sm-dispatcher] designed #{number}: speaking-lane spawn "
            f"machinery not wired — leaving at sm:designed"
        )
        return

    if has_live_speaking_spawn(number):
        log(
            f"[sm-dispatcher] designed #{number}: live speaking spawn "
            f"dir already running — skipping spawn"
        )
        return

    live = count_running_speaking()
    if live >= max_concurrent_speaking_spawns:
        log(
            f"[sm-dispatcher] designed #{number}: speaking concurrency "
            f"cap reached ({live}/{max_concurrent_speaking_spawns}) — "
            f"queued for next pass"
        )
        return

    reason = "build-started: speaking-agent spawned"
    transition_body = render_transition_comment(
        DESIGNED_SM_LABEL, BUILDING_SM_LABEL, reason
    )

    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would spawn speaking on #{number} "
            f"art={art_label} "
            f"(running={live}/{max_concurrent_speaking_spawns}) and "
            f"transition designed → building"
        )
        report.spawned += 1
        report.spawn_records.append((number, art_label, "<dry-run>"))
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNED_SM_LABEL, BUILDING_SM_LABEL)
        )
        return

    # Issue #327 — resolve the design-ready slug to a real filesystem
    # path so the speaking-agent can load the approved design. Without
    # this, ``spawn_speaking_agent`` is invoked with
    # ``design_note_path=None`` and ``compose_speaking_spawn_prompt``
    # renders ``design_note: (unset)`` in the frontmatter; the build
    # worker then crashes with ``design_note path does not exist:
    # (unset)`` and the SM has no dead-worker detector at sm:building
    # (#298/EC-3), so the issue stays stuck.
    design_note_path: pathlib.Path | None = None
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: failed to list "
            f"comments while resolving design-note path: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        comments = []
    slug = _find_design_ready_slug(comments, trusted_authors=trusted_authors)
    if slug is not None:
        design_note_path = _resolve_design_note_path(slug)
        if design_note_path is None:
            log(
                f"[sm-dispatcher] designed #{number}: design-ready "
                f"slug=[[{slug}]] does not resolve under "
                f"{DESIGNS_DIR} or {RESEARCH_NOTES_DIR} — spawning "
                f"with design_note_path=None (build will likely fail)"
            )
    else:
        log(
            f"[sm-dispatcher] designed #{number}: no [SM] design-ready "
            f"comment from a trusted author — spawning with "
            f"design_note_path=None"
        )

    # Spawn first — the speaking-agent posts its own
    # [SM] speaking-spawn-started audit comment before launching the
    # shim, so failure to spawn leaves a recoverable audit trail and
    # doesn't move the label.
    try:
        spawn_id = spawn_speaking(
            issue, art_label, repo, design_note_path=design_note_path
        )
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: failed to spawn "
            f"speaking-agent: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: speaking spawn "
            f"OS error: {exc}"
        )
        return
    if spawn_id is None:
        return
    report.spawned += 1
    report.spawn_records.append((number, art_label, spawn_id))

    # Transition designed → building so _process_building picks the
    # draft PR up on the next pass.
    try:
        edit_labels(
            repo,
            number,
            add=[BUILDING_SM_LABEL],
            remove=[DESIGNED_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: "
            f"failed to transition to building: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNED_SM_LABEL, BUILDING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designed → building (speaking spawn_id={spawn_id})"
    )
