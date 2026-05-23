"""Handler for ``sm:building`` issues — promotes the issue to ``sm:reviewing`` once a linked PR shows up.
"""

from __future__ import annotations

from alice_forge.sm.legacy.handlers._common import *  # noqa: F401, F403


def _process_building(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    has_live_speaking_spawn: Callable[[int], bool] | None,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:building → sm:reviewing once a linked PR appears, or
    sm:blocked if the build worker died silently (issue #342).

    Mirrors the T1 sub-path inside :func:`_process_selected`: an
    open linked PR is the "build complete" signal. The build-phase
    agent opens its PR as a draft (per ``per-issue-build.md``); the
    dispatcher relabels and hands off to the existing reviewing-state
    pipeline (CI + verify + Sonnet review).

    Dead-worker guard (issue #342, EC-3 leftover from #298): when a
    ``[SM] speaking-spawn-started`` audit was posted from a trusted
    author but no linked PR has appeared AND no live speaking spawn
    dir remains, the build worker exited without opening a PR — same
    shape as #202's silent-spawn-failure guard at sm:selected.
    Transition to ``sm:blocked`` rather than letting the issue sit at
    sm:building forever. Observed on #294/#296/#297/#323 on
    2026-05-23 — build workers crashed silently and the issues sat
    stuck until a human noticed.
    """
    number = issue["number"]
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] building #{number}: "
            f"failed to look up linked PR: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is None:
        # Issue #342 — silent build-spawn guard. Mirrors the thinking
        # lane's guard from #202.
        if has_live_speaking_spawn is not None and not has_live_speaking_spawn(
            number
        ):
            try:
                bld_comments = list_comments(repo, number)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] building #{number}: "
                    f"failed to list comments for dead-worker check: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                bld_comments = []
            saw_speaking_spawn_started = False
            for c in bld_comments:
                if not isinstance(c, dict):
                    continue
                body = c.get("body")
                if not isinstance(body, str):
                    continue
                if not body.startswith(SPEAKING_SPAWN_STARTED_PREFIX):
                    continue
                login = _comment_author_login(c)
                if not isinstance(login, str) or login not in trusted_authors:
                    continue
                saw_speaking_spawn_started = True
                break
            if saw_speaking_spawn_started:
                reason = (
                    "speaking-agent exited without opening a PR "
                    "(see #342)"
                )
                transition_body = render_transition_comment(
                    BUILDING_SM_LABEL, BLOCKED_SM_LABEL, reason
                )
                if dry_run:
                    log(
                        f"[sm-dispatcher] DRY-RUN would transition "
                        f"#{number}: building → blocked ({reason})"
                    )
                    report.transitioned += 1
                    report.transitions.append(
                        (number, BUILDING_SM_LABEL, BLOCKED_SM_LABEL)
                    )
                    return
                try:
                    edit_labels(
                        repo,
                        number,
                        add=[BLOCKED_SM_LABEL],
                        remove=[BUILDING_SM_LABEL],
                    )
                    post_comment(repo, number, transition_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] building #{number}: "
                        f"failed silent-build-failure transition: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    return
                report.transitioned += 1
                report.transitions.append(
                    (number, BUILDING_SM_LABEL, BLOCKED_SM_LABEL)
                )
                log(
                    f"[sm-dispatcher] transitioned #{number}: "
                    f"building → blocked ({reason})"
                )
                return
        log(
            f"[sm-dispatcher] building #{number}: "
            f"no linked PR yet — staying"
        )
        return
    pr_state = (pr.get("state") or "").upper()
    if pr_state != "OPEN":
        log(
            f"[sm-dispatcher] building #{number}: linked PR is "
            f"{pr_state!r} (not OPEN) — not transitioning"
        )
        return

    pr_url = pr.get("url") or "<unknown>"
    reason = f"PR opened: {pr_url}"
    transition_body = render_transition_comment(
        BUILDING_SM_LABEL, REVIEWING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"building → reviewing ({pr_url})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, BUILDING_SM_LABEL, REVIEWING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[REVIEWING_SM_LABEL],
            remove=[BUILDING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] building #{number}: "
            f"failed to transition to reviewing: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, BUILDING_SM_LABEL, REVIEWING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"building → reviewing ({pr_url})"
    )
