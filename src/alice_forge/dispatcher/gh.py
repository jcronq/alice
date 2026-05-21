"""``gh`` CLI shims for the dispatcher.

Every interaction with the ``gh`` binary funnels through :func:`_run_gh`,
which raises :class:`GHCommandError` on non-zero exit so the main loop
can detect auth failure / rate-limit and bail without writing partial
state.

The ``gh_*`` helpers are deliberately thin — one helper per JSON-shaped
``gh`` call — so tests can monkeypatch a single helper (or, more
commonly, inject a fake via the ``list_issues=`` / ``post_comment=``
kwargs on :func:`alice_forge.dispatcher.run`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable, Iterable

from alice_forge.dispatcher.constants import (
    ACTIVE_SM_LABEL,
    DONE_SM_LABEL,
    NON_TERMINAL_SM_LABELS,
    RECENT_ISSUE_LIMIT,
    SM_LABEL_WHITELIST,
    SPAWN_STARTED_PREFIX,
    TRUSTED_AUTHORS,
)
from alice_forge.dispatcher.errors import GHCommandError
from alice_forge.dispatcher.trust import _label_names


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Invoke ``gh`` with the given args, raise GHCommandError on failure.

    Returns stdout as a string. Empty stdout is returned as ``""``.
    """
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GHCommandError(returncode=-1, stderr=str(exc), args=args) from exc
    if result.returncode != 0:
        raise GHCommandError(
            returncode=result.returncode,
            stderr=result.stderr or result.stdout,
            args=args,
        )
    return result.stdout


def _dispatch_run_gh(args: list[str], **kwargs: Any) -> str:
    """Call ``_run_gh`` through the parent dispatcher module's namespace.

    Tests monkeypatch ``alice_forge.dispatcher._run_gh`` directly (see
    e.g. ``test_gh_get_pr_files_parses_payload``). Pre-split, every
    ``gh_*`` helper resolved ``_run_gh`` via a global lookup in
    ``alice_forge.dispatcher``'s own module dict, so the monkeypatch took
    effect. After the split, naive callers inside this submodule would
    bind ``_run_gh`` from ``gh.py``'s globals — invisible to the
    monkeypatch. This indirection routes each call back through the
    dispatcher module so the test contract is preserved.
    """
    return sys.modules["alice_forge.dispatcher"]._run_gh(args, **kwargs)


def _sort_oldest_first(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # FIFO: oldest createdAt first so the concurrency cap is fair and
    # new arrivals don't starve queued tasks. Issues without a
    # createdAt sort last (treated as "newer than any timestamped
    # peer") so a malformed payload can't silently jump the queue.
    return sorted(
        issues,
        key=lambda i: (i.get("createdAt") or "9999-12-31T23:59:59Z", i.get("number", 0)),
    )


def gh_list_selected_issues(repo: str, *, gh_bin: str = "gh") -> list[dict[str, Any]]:
    """Return open ``sm:selected`` issues. v0 helper, retained for compat.

    Phase 1.5's actual main poll uses :func:`gh_list_sm_issues` and
    filters by label client-side so the same payload covers
    ``sm:reviewing``, ``sm:building``, etc.
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--label",
        ACTIVE_SM_LABEL,
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    return _sort_oldest_first(payload)


def gh_list_sm_issues(repo: str, *, gh_bin: str = "gh") -> list[dict[str, Any]]:
    """Return all open issues with any ``sm:*`` label.

    ``gh issue list`` doesn't have an "OR across labels" flag; we use
    ``--search`` with ``label:sm:selected,sm:reviewing,...`` (comma is
    OR in the GitHub search syntax for the label qualifier when
    repeated). Simpler: pull all open issues at once and filter
    client-side. RECENT_ISSUE_LIMIT keeps the payload bounded.
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        "label:sm:draft,sm:needs_study,sm:selected,sm:designing,sm:design_review,sm:designed,sm:compacting,sm:building,sm:reviewing,sm:validating",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    # Defensive client-side filter: the search qualifier above is OR
    # across the listed labels, but if gh ever loosens parsing we still
    # only act on issues with at least one whitelisted ``sm:*`` label.
    filtered = [
        issue
        for issue in payload
        if any(n in SM_LABEL_WHITELIST for n in _label_names(issue))
    ]
    return _sort_oldest_first(filtered)


def gh_list_stale_closed_sm_issues(
    repo: str, *, gh_bin: str = "gh"
) -> list[dict[str, Any]]:
    """Return closed issues that still carry a non-terminal ``sm:*`` label.

    Phase 1.6 sweep target: when a PR with ``Closes #N`` merges fast
    enough that the dispatcher's open-PR window is missed, GitHub
    auto-closes the issue but leaves its ``sm:*`` label at whatever it
    was (typically ``sm:selected``). The main poll filters ``--state
    open`` and never sees the closed issue. This helper finds those
    strays so :func:`_process_stale_closed` can route them to the
    correct terminal state.

    Same ``--search`` OR-syntax trick as :func:`gh_list_sm_issues`,
    scoped to ``--state closed`` and to non-terminal ``sm:*`` labels.
    Defense-in-depth: also filters client-side, so a relaxed gh parse
    or stale label cache can't pull a terminal-labeled issue into the
    sweep.
    """
    search_terms = ",".join(sorted(NON_TERMINAL_SM_LABELS))
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "closed",
        "--search",
        f"label:{search_terms}",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    # Client-side defense: only keep issues whose label set contains at
    # least one *non-terminal* whitelisted ``sm:*`` label. A closed
    # issue at ``sm:done`` must never appear here even if the search
    # qualifier loosens upstream.
    return [
        issue
        for issue in payload
        if any(n in NON_TERMINAL_SM_LABELS for n in _label_names(issue))
    ]


def gh_list_open_done_sm_issues(
    repo: str, *, gh_bin: str = "gh"
) -> list[dict[str, Any]]:
    """Return OPEN issues that carry ``sm:done`` — the #174 close-stragglers.

    The ``art:research_note`` worker flips ``sm:selected → sm:done`` directly
    (no PR, no ``sm:reviewing`` pit-stop), so the main open-issue poll —
    which only searches non-terminal ``sm:*`` labels — never sees these
    issues again and ``gh issue close`` never fires. The result: research
    items look "failed" in the viewer because the card stays in the open
    list while the work is actually done.

    This helper is the open-side companion to
    :func:`gh_list_stale_closed_sm_issues`: it returns OPEN issues whose
    ``sm:*`` label is *terminal*. The caller (:func:`_process_open_done`)
    re-validates the artifact and enforces the
    ``[SM] exit-transition`` gate before closing.
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        f"label:{DONE_SM_LABEL}",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    # Client-side defense: only keep issues whose label set contains
    # ``sm:done``. A loosened search qualifier upstream must not pull
    # in unrelated issues.
    return [
        issue
        for issue in payload
        if DONE_SM_LABEL in _label_names(issue)
    ]


def gh_post_comment(repo: str, number: int, body: str, *, gh_bin: str = "gh") -> None:
    """Post a comment on an issue via ``gh issue comment``."""
    args = [
        gh_bin,
        "issue",
        "comment",
        str(number),
        "--repo",
        repo,
        "--body",
        body,
    ]
    _dispatch_run_gh(args)


def gh_edit_labels(
    repo: str,
    number: int,
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    gh_bin: str = "gh",
) -> None:
    """Add/remove labels on an issue via ``gh issue edit``."""
    args = [gh_bin, "issue", "edit", str(number), "--repo", repo]
    for label in add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    if len(args) == 6:
        # No-op: caller passed empty add/remove. Don't shell out.
        return
    _dispatch_run_gh(args)


def gh_close_issue(repo: str, number: int, *, gh_bin: str = "gh") -> None:
    """Close an issue via ``gh issue close``."""
    args = [gh_bin, "issue", "close", str(number), "--repo", repo]
    _dispatch_run_gh(args)


def gh_get_issue(
    repo: str, number: int, *, gh_bin: str = "gh"
) -> dict[str, Any] | None:
    """Fetch a single issue's state + labels via ``gh issue view``.

    Issue #142 — the proactive reap pass needs to know whether the
    issue behind a dead spawn dir has reached a terminal state (CLOSED
    / sm:done / sm:rejected). ``gh_list_sm_issues`` only returns OPEN
    issues, so we can't reuse the polled list to answer that question.

    Returns the raw ``{"number", "state", "labels"}`` payload, or
    ``None`` if the issue doesn't exist (404 / repo permission error /
    transport failure). A ``None`` return is the caller's signal to
    leave the spawn dir alone for this cycle and retry on the next
    pass.
    """
    args = [
        gh_bin,
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,state,labels",
    ]
    try:
        stdout = _dispatch_run_gh(args)
    except GHCommandError:
        return None
    if not stdout.strip():
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def gh_list_issue_comments(
    repo: str, number: int, *, gh_bin: str = "gh"
) -> list[dict[str, Any]]:
    """Return the comment list for an issue via ``gh issue view``.

    Each entry has ``body`` and ``author.login``. Used by
    :func:`gh_find_unspawned_selected_issues` to check for the
    ``[SM] spawn-started`` audit comment.
    """
    args = [
        gh_bin,
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "comments",
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return []
    raw = payload.get("comments") or []
    if not isinstance(raw, list):
        return []
    return raw


def gh_find_unspawned_selected_issues(
    repo: str,
    *,
    list_issues: Callable[[str], list[dict[str, Any]]] | None = None,
    list_comments: Callable[[str, int], list[dict[str, Any]]] | None = None,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    spawn_prefix: str = SPAWN_STARTED_PREFIX,
) -> list[dict[str, Any]]:
    """Return open ``sm:selected`` issues with no ``[SM] spawn-started`` comment.

    Phase 2 dedup primitive — paired with :func:`spawn_agent`. The
    "we've already spawned" signal is a comment whose body starts with
    :data:`SPAWN_STARTED_PREFIX` and whose author is in
    ``trusted_authors`` (so a random commenter typing the prefix
    can't trick the dispatcher into skipping a real task).

    Both ``list_issues`` and ``list_comments`` are injectable for tests.
    """
    if list_issues is None:
        list_issues = gh_list_selected_issues
    if list_comments is None:
        list_comments = gh_list_issue_comments

    candidates = list_issues(repo)
    unspawned: list[dict[str, Any]] = []
    for issue in candidates:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        try:
            comments = list_comments(repo, number)
        except GHCommandError:
            # Defer to caller's error handling — re-raise so the main
            # loop can detect auth/rate-limit and bail. For other
            # transient errors the caller's outer try/except will skip
            # this issue.
            raise
        already_spawned = False
        for c in comments:
            body = c.get("body") if isinstance(c, dict) else None
            author = c.get("author") if isinstance(c, dict) else None
            if isinstance(author, dict):
                login = author.get("login")
            elif isinstance(author, str):
                login = author
            else:
                login = None
            if (
                isinstance(body, str)
                and body.startswith(spawn_prefix)
                and isinstance(login, str)
                and login in trusted_authors
            ):
                already_spawned = True
                break
        if not already_spawned:
            unspawned.append(issue)
    return unspawned


def gh_find_linked_pr(
    repo: str, issue_number: int, *, gh_bin: str = "gh"
) -> dict[str, Any] | None:
    """Return the first PR referencing this issue, or None.

    Uses ``gh pr list --search "linked:issue"`` (which returns PRs that
    have a "Closes #N"-style link) and filters by
    ``closingIssuesReferences`` containing the issue number. First
    match wins; later phases may need ordering rules.

    Queries ``--state all`` so callers in the T2/T3 path can find the
    linked PR after it has merged. Callers in the T1 path (sm:selected
    → sm:reviewing) must filter by the returned ``state`` field — T1
    should only fire when the linked PR is still ``OPEN``.
    """
    args = [
        gh_bin,
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--search",
        "linked:issue",
        "--json",
        "number,url,state,closingIssuesReferences",
        "--limit",
        "100",
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return None
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return None
    for pr in payload:
        refs = pr.get("closingIssuesReferences") or []
        for ref in refs:
            if isinstance(ref, dict) and ref.get("number") == issue_number:
                return {
                    "number": pr.get("number"),
                    "url": pr.get("url"),
                    "state": pr.get("state"),
                }
    return None


def gh_get_pr_merge_status(
    repo: str, pr_number: int, *, gh_bin: str = "gh"
) -> dict[str, Any]:
    """Return ``{merged, merge_commit_oid, pr_url, head_ref_name}`` for a PR.

    ``head_ref_name`` is the source branch (the worker's feature branch
    for SM-spawned PRs) — Issue #127 uses it to delete the merged local
    branch during post-merge cleanup. ``None`` if the gh payload didn't
    return it (defensive against schema drift).
    """
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "state,mergeCommit,url,headRefName",
    ]
    stdout = _dispatch_run_gh(args)
    empty = {
        "merged": False,
        "merge_commit_oid": None,
        "pr_url": None,
        "head_ref_name": None,
    }
    if not stdout.strip():
        return empty
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return empty
    merge_commit = payload.get("mergeCommit") or {}
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    head_ref = payload.get("headRefName")
    return {
        "merged": payload.get("state") == "MERGED",
        "merge_commit_oid": oid,
        "pr_url": payload.get("url"),
        "head_ref_name": head_ref if isinstance(head_ref, str) and head_ref else None,
    }


def gh_get_pr_mergeable(
    repo: str, pr_number: int, *, gh_bin: str = "gh"
) -> dict[str, Any]:
    """Return ``{mergeable, head_ref_name, head_ref_oid}`` for an open PR.

    Issue #173 — the dispatcher uses this at ``sm:reviewing`` when the
    PR is still open to decide whether to attempt an auto-rebase.

    ``mergeable`` is one of:
      * ``"MERGEABLE"`` — clean merge possible
      * ``"CONFLICTING"`` — needs rebase / manual resolution
      * ``"UNKNOWN"``    — GitHub is still computing
      * ``None``         — gh returned no payload (treat as UNKNOWN)
    """
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "mergeable,headRefName,headRefOid",
    ]
    stdout = _dispatch_run_gh(args)
    empty = {"mergeable": None, "head_ref_name": None, "head_ref_oid": None}
    if not stdout.strip():
        return empty
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return empty
    head_ref = payload.get("headRefName")
    head_oid = payload.get("headRefOid")
    return {
        "mergeable": payload.get("mergeable"),
        "head_ref_name": head_ref if isinstance(head_ref, str) and head_ref else None,
        "head_ref_oid": head_oid if isinstance(head_oid, str) and head_oid else None,
    }


def gh_get_master_ci_status(
    repo: str, commit_sha: str, *, gh_bin: str = "gh"
) -> dict[str, Any]:
    """Return master CI status for a specific commit.

    Returns ``{conclusion, run_url}`` where ``conclusion`` is:
      - ``"success"`` — all completed runs succeeded
      - ``"failure"`` — at least one completed run failed/cancelled/timed_out
      - ``"pending"`` — at least one run still in_progress/queued
      - ``None``     — no runs found (yet)
    """
    args = [
        gh_bin,
        "run",
        "list",
        "--repo",
        repo,
        "--branch",
        "master",
        "--commit",
        commit_sha,
        "--json",
        "conclusion,status,url",
        "--limit",
        "5",
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return {"conclusion": None, "run_url": None}
    payload = json.loads(stdout)
    if not isinstance(payload, list) or not payload:
        return {"conclusion": None, "run_url": None}

    failure_url: str | None = None
    pending = False
    for run in payload:
        status = (run.get("status") or "").lower()
        conclusion = (run.get("conclusion") or "").lower()
        url = run.get("url")
        # GitHub statuses: queued, in_progress, completed.
        if status != "completed":
            pending = True
            continue
        # Completed: conclusion is success / failure / cancelled /
        # timed_out / skipped / neutral / action_required.
        if conclusion in ("success", "skipped", "neutral"):
            continue
        # Anything else completed-but-not-green is a failure.
        if failure_url is None:
            failure_url = url

    if failure_url is not None:
        # Failure dominates: a single red run is enough to gate on.
        return {"conclusion": "failure", "run_url": failure_url}
    if pending:
        return {"conclusion": "pending", "run_url": None}
    # All completed runs were success/skipped/neutral.
    first_url = payload[0].get("url") if isinstance(payload[0], dict) else None
    return {"conclusion": "success", "run_url": first_url}


def gh_get_pr_files(
    repo: str, pr_number: int, *, gh_bin: str = "gh"
) -> list[str]:
    """Return the list of file paths changed by a PR.

    Used by the issue #128 verification step to decide whether the
    viewer-route smoke test applies (any path under
    ``src/viewer/`` flips the recipe on). An empty list on a
    successful call is legal (a PR with only renames-as-deletes is
    unusual but not impossible); the verifier treats empty as "no
    viewer touch → skip", which is the safe default.
    """
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "files",
    ]
    stdout = _dispatch_run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return []
    raw = payload.get("files") or []
    if not isinstance(raw, list):
        return []
    paths: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            p = entry.get("path")
            if isinstance(p, str):
                paths.append(p)
    return paths
