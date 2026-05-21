"""Handler for ``sm:draft`` issues — converts a trusted ``[SM] route-to-study`` comment into the ``sm:draft → sm:needs_study`` transition.

When the handler encounters a draft with no parsed RouteToStudy comment, it writes a one-shot triage surface to ``inner/surface/`` so Speaking can decide (route-to-study or close-as-rejected) instead of the draft sitting silently for days (issue #235). Dedup is enforced by :class:`DispatcherState.triage_surfaced` so the surface fires exactly once per draft entry.
"""

from __future__ import annotations

import datetime as _dt
import re as _re

from alice_forge.dispatcher.handlers._common import *  # noqa: F401, F403


_SLUG_RE = _re.compile(r"[^a-z0-9]+")


def _slugify_repo(repo: str) -> str:
    """Turn ``owner/repo`` into ``owner-repo`` for filenames. Lossy on
    purpose — the issue number disambiguates so a perfect round-trip
    isn't needed."""
    return _SLUG_RE.sub("-", repo.lower()).strip("-") or "repo"


def _truncate_body(body: str, limit: int) -> str:
    """Trim an issue body to ``limit`` characters, appending a marker
    when truncated. Empty body collapses to an explicit placeholder so
    the surface still parses as well-formed."""
    if not body:
        return "_(empty body)_"
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + f"\n\n…(truncated; full body in issue payload — {len(body)} chars total)"


def _render_triage_surface(
    *,
    issue: dict[str, Any],
    repo: str,
    art_label: str | None,
    now: _dt.datetime,
    body_char_limit: int,
) -> str:
    """Render the triage-surface markdown body.

    Frontmatter mirrors the existing surface contract Speaking already
    understands (``priority`` / ``context`` / ``reply_expected``) plus the
    ``action: triage-sm-draft`` discriminator and the issue payload
    (number, url, title, author) so Speaking doesn't have to re-fetch
    just to decide. ``source-id`` + ``date`` feed the intake dedup window
    in :class:`alice_speaking.internal.surfaces.SurfaceWatcher`.
    """
    number = issue["number"]
    title = issue.get("title") or "(no title)"
    body = issue.get("body") or ""
    author_field = issue.get("author")
    if isinstance(author_field, dict):
        author = author_field.get("login") or "unknown"
    elif isinstance(author_field, str):
        author = author_field
    else:
        author = "unknown"
    issue_url = f"https://github.com/{repo}/issues/{number}"
    date_str = now.strftime("%Y-%m-%d")
    source_id = f"sm-dispatcher-triage-{_slugify_repo(repo)}-{number}"
    art_line = art_label if art_label is not None else "(none)"
    truncated_body = _truncate_body(body, body_char_limit)
    # Quote the title so a colon in the title doesn't break YAML parsing.
    title_yaml = title.replace('"', '\\"')
    return (
        "---\n"
        "priority: triage\n"
        f"context: sm:draft awaiting triage — {repo}#{number}\n"
        "reply_expected: true\n"
        "action: triage-sm-draft\n"
        f"repo: {repo}\n"
        f"issue_number: {number}\n"
        f"issue_url: {issue_url}\n"
        f'issue_title: "{title_yaml}"\n'
        f"author: {author}\n"
        f"art_label: {art_line}\n"
        f"source-id: {source_id}\n"
        f"date: {date_str}\n"
        "---\n"
        "\n"
        f"@{author} filed {repo}#{number} at `sm:draft` and the dispatcher "
        "has been waiting for a trusted `[SM] route-to-study` comment that "
        "never came. No other code path wakes Speaking for an idle draft, "
        "so this surface asks you to triage it directly.\n"
        "\n"
        f"**Title:** {title}\n"
        "\n"
        f"**Current art label:** {art_line}\n"
        "\n"
        "**Body:**\n"
        f"{truncated_body}\n"
        "\n"
        "**Decide one (then `resolve_surface` this file):**\n"
        f"1. Advance to study — `gh issue comment {number} --repo {repo} "
        '--body "[SM] route-to-study"`  '
        "(append ` art=art:<label>` to swap the artifact label at the "
        "same time).\n"
        f"2. Close as rejected — `gh issue edit {number} --repo {repo} "
        "--add-label sm:rejected --remove-label sm:draft` then post a "
        "short reason comment and `gh issue close`.\n"
        "3. Park elsewhere (blocked, deferred, etc.) — your call; the "
        "dispatcher only re-emits this surface if the issue re-enters "
        "`sm:draft` after Speaking moves it out.\n"
    )


def _write_triage_surface(
    *,
    issue: dict[str, Any],
    repo: str,
    art_label: str | None,
    surface_dir: pathlib.Path,
    now: _dt.datetime,
    body_char_limit: int,
) -> pathlib.Path:
    """Write the rendered triage surface under ``surface_dir`` and return its path.

    Filename matches the existing surface convention used elsewhere:
    ``<YYYY-MM-DD-HHMMSS>-<slug>.md``. The slug encodes the repo and
    issue number so the operator (and the dedup key parser) can
    identify the surface at a glance.
    """
    surface_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    slug = f"sm-dispatcher-triage-{_slugify_repo(repo)}-{issue['number']}"
    path = surface_dir / f"{stamp}-{slug}.md"
    content = _render_triage_surface(
        issue=issue,
        repo=repo,
        art_label=art_label,
        now=now,
        body_char_limit=body_char_limit,
    )
    path.write_text(content, encoding="utf-8")
    return path


def _parse_now_iso(now_iso_str: str) -> _dt.datetime:
    """Parse the dispatcher's ISO-8601 ``now_iso()`` string back into a datetime.

    The ``_now_iso`` helper emits ``YYYY-MM-DDTHH:MM:SS+00:00``; ``fromisoformat``
    accepts that shape natively. Returns an aware datetime so ``strftime`` is
    deterministic across host timezones.
    """
    return _dt.datetime.fromisoformat(now_iso_str)


def _process_draft(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    art_whitelist: frozenset[str],
    surface_dir: pathlib.Path,
    body_char_limit: int,
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:draft → sm:needs_study on a trusted ``[SM] route-to-study`` comment.

    The ``art=<art-label>`` field is optional. When present *and*
    different from the issue's current ``art:*`` label, the dispatcher
    swaps the label atomically with the state transition.

    Issue #235: when there's no trusted RouteToStudy comment, write a
    one-shot triage surface to ``surface_dir`` so Speaking can decide
    (advance to study or close-as-rejected) instead of the draft sitting
    silently. The dedup ledger is :attr:`DispatcherState.triage_surfaced`.
    """
    number = issue["number"]
    decision = evaluate_trust(issue, trusted_authors=trusted_authors)
    if not decision.accepted:
        log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
        report.skipped_trust += 1
        return

    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] draft #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_forge.comments import RouteToStudy

    parsed = _find_parsed_comment_of_type(
        comments,
        RouteToStudy,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        # Issue #235 — no trusted RouteToStudy comment yet. The draft
        # would otherwise sit silently; emit one triage surface so
        # Speaking can route it. Dedup on the ledger so re-runs of the
        # 60-second poll don't spam the surface dir.
        if state.has_triage_surfaced(number):
            return
        current_art = _current_art_label(issue, art_whitelist)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would emit triage surface for "
                f"draft #{number}"
            )
            report.triage_surfaced += 1
            return
        try:
            now = _parse_now_iso(now_iso())
        except ValueError:
            now = _dt.datetime.now(_dt.timezone.utc)
        try:
            surface_path = _write_triage_surface(
                issue=issue,
                repo=repo,
                art_label=current_art,
                surface_dir=surface_dir,
                now=now,
                body_char_limit=body_char_limit,
            )
        except OSError as exc:
            log(
                f"[sm-dispatcher] draft #{number}: "
                f"failed to write triage surface under {surface_dir}: {exc}"
            )
            # Don't mark the ledger — next pass retries.
            return
        state.mark_triage_surfaced(number)
        report.triage_surfaced += 1
        log(
            f"[sm-dispatcher] draft #{number}: triage surface written at "
            f"{surface_path}"
        )
        return

    add_labels = [NEEDS_STUDY_SM_LABEL]
    remove_labels = [DRAFT_SM_LABEL]
    reason = "route-to-study"
    if parsed.art_label is not None:
        current_art = _current_art_label(issue, art_whitelist)
        if parsed.art_label != current_art:
            add_labels.append(parsed.art_label)
            if current_art is not None:
                remove_labels.append(current_art)
        reason += f" art={parsed.art_label}"

    transition_body = render_transition_comment(
        DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"draft → needs_study ({reason})"
        )
        report.transitioned += 1
        report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
        return
    try:
        edit_labels(repo, number, add=add_labels, remove=remove_labels)
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] draft #{number}: "
            f"failed route-to-study transition: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    # Issue #235: clearing the triage ledger here means a future
    # re-entry into sm:draft (e.g. operator relabels a closed draft)
    # gets a fresh surface rather than silent skipping.
    state.clear_triage_surfaced(number)
    report.transitioned += 1
    report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"draft → needs_study ({reason})"
    )
