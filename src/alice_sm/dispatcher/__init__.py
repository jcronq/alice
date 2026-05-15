"""State Machine v0/v1.5/v2 dispatcher — ``gh``-driven label-driven dispatcher.

Modeled on :mod:`alice_watchers.github`. Each invocation is a single pass:

  1. Poll ``jcronq/alice`` for open issues with any ``sm:*`` label
     (``gh issue list ... --json number,title,labels,author,...``).
  2. For ``sm:selected`` issues:
     - Apply the v0 trust filter — author whitelist, exactly one
       ``sm:*`` label, at least one ``art:*`` label — all from explicit
       allow-lists so a typo (``sm:building-pleaserun``) is silently
       dropped instead of producing a fuzzy match.
     - For each unseen passing issue, post a one-time
       ``[SM] dispatcher-hello ...`` comment as audit-trail evidence
       and record the issue number in
       ``/state/worker/sm-dispatcher-state.json`` so we don't
       re-comment on the next cadence.
     - If a linked open PR exists, transition to ``sm:reviewing``
       (Phase 1.5 T1). Hello + transition can co-occur in one pass.
     - Phase 2: if the issue has not already been spawned on (no
       ``[SM] spawn-started`` comment from a trusted author), and the
       global concurrency cap has room, spawn a detached ``claude``
       CLI subprocess to actually do the work. The spawn comment is
       posted *before* the Popen so the next pass sees the dedup
       marker even if the spawn crashes immediately.
  3. For ``sm:reviewing`` issues (Phase 1.5 T2/T3):
     - If the linked PR is merged AND master CI on the merge commit
       is green → relabel ``sm:done``, close the issue.
     - If the linked PR is merged AND master CI is red → relabel
       ``sm:building`` (do NOT close, do NOT spawn anything yet).
     - If still pending or PR still open, stay.

Phase 2 adds agent spawning but does NOT handle the persona × runtime
matrix (everything spawns Claude CLI), amendments in-flight, or
session continuity across review cycles. Those land in later phases.

The script is intended to be invoked on a cadence by s6 (later phase);
right now it runs by hand via ``python -m alice_sm.dispatcher``. The
``--dry-run`` flag prints the comments / transitions / spawns that
would be made without touching GitHub or launching subprocesses —
useful for tests and manual verification.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Constants — extracted to alice_sm.dispatcher.constants (issue #193).
# Re-exported so ``from alice_sm.dispatcher import X`` keeps working.
#
# If ``constants`` is already loaded when this package is re-executed
# (``importlib.reload(alice_sm.dispatcher)``), reload it first so the
# env-driven caps (MAX_CONCURRENT_*_SPAWNS) refresh against the current
# environment. The two existing reload-pattern tests
# (test_thinking_spawn_concurrency_cap_constant_and_env_override,
# test_speaking_spawn_concurrency_cap_constant_and_env_override) depend
# on this — pre-split, both constants lived in ``dispatcher.py`` and
# reloading the module picked up env changes directly.
# ---------------------------------------------------------------------------
import importlib as _importlib

_constants_modname = __name__ + ".constants"
if _constants_modname in sys.modules:
    _importlib.reload(sys.modules[_constants_modname])
del _importlib, _constants_modname

from alice_sm.dispatcher.constants import *  # noqa: F401, F403, E402
from alice_sm.dispatcher.constants import _now_iso  # noqa: F401, E402



# ---------------------------------------------------------------------------
# Errors — extracted to alice_sm.dispatcher.errors (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.errors import GHCommandError  # noqa: E402, F401


# ---------------------------------------------------------------------------
# State load/save — extracted to alice_sm.dispatcher.state (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.state import (  # noqa: E402, F401
    DispatcherState,
    load_state,
    save_state,
)



# ---------------------------------------------------------------------------
# gh CLI shims — extracted to alice_sm.dispatcher.gh (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.gh import (  # noqa: E402, F401
    _run_gh,
    _sort_oldest_first,
    gh_close_issue,
    gh_edit_labels,
    gh_find_linked_pr,
    gh_find_unspawned_selected_issues,
    gh_get_issue,
    gh_get_master_ci_status,
    gh_get_pr_files,
    gh_get_pr_mergeable,
    gh_get_pr_merge_status,
    gh_list_issue_comments,
    gh_list_open_done_sm_issues,
    gh_list_selected_issues,
    gh_list_sm_issues,
    gh_list_stale_closed_sm_issues,
    gh_post_comment,
)




# Callable type aliases — extracted to alice_sm.dispatcher.types (issue #193).
from alice_sm.dispatcher.types import (  # noqa: E402, F401
    CloseIssueFn,
    EditLabelsFn,
    FindLinkedPRFn,
    FindUnspawnedFn,
    GitRunFn,
    ListCommentsFn,
    ListIssuesFn,
    MasterCIStatusFn,
    PostCommentFn,
    PostMergeCleanupFn,
    PRFilesFn,
    PRMergeableFn,
    PRMergeStatusFn,
    VerifyFn,
)


# ---------------------------------------------------------------------------
# Git operations — extracted to alice_sm.dispatcher.git_ops (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.git_ops import (  # noqa: E402, F401
    _REBASE_CONFLICT_FILE_RE,
    _attempt_auto_rebase,
    _extract_rebase_conflict_file,
    _post_merge_cleanup,
    _run_git,
)



# ---------------------------------------------------------------------------
# Spawn machinery — extracted to alice_sm.dispatcher.spawn (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.spawn import (  # noqa: E402, F401
    _SPAWN_DIR_NAME_RE,
    _copy_session_jsonl_into_spawn,
    _find_worker_session_jsonl,
    _reap_spawn_dir,
    _spawn_dir_is_alive,
    _spawn_dir_issue_number,
    compose_rebase_prompt,
    compose_speaking_spawn_prompt,
    compose_spawn_prompt,
    compose_thinking_spawn_prompt,
    count_running_speaking_spawns,
    count_running_spawns,
    count_running_thinking_spawns,
    find_live_spawn_dir_for_issue,
    has_live_spawn_for_issue,
    has_live_speaking_spawn_for_issue,
    has_live_thinking_spawn_for_issue,
    proactive_reap_dead_spawns,
    render_spawn_started_comment,
    render_speaking_spawn_started_comment,
    render_thinking_spawn_started_comment,
    resolve_claude_bin,
    resolve_python_bin,
    spawn_agent,
    spawn_rebase_agent,
    spawn_speaking_agent,
    spawn_thinking_agent,
)



# ---------------------------------------------------------------------------
# Trust filter — extracted to alice_sm.dispatcher.trust (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.trust import (  # noqa: E402, F401
    TrustDecision,
    _author_login,
    _current_sm_label,
    _label_names,
    evaluate_trust,
)




# ---------------------------------------------------------------------------
# Comment rendering — extracted to alice_sm.dispatcher.rendering (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.rendering import (  # noqa: E402, F401
    REBASE_ESCALATED_PREFIX,
    REBASE_NEEDED_PREFIX,
    REBASE_PUSHED_PREFIX,
    render_auto_study_complete_comment,
    render_design_ready_audit_comment,
    render_design_revisions_capped_comment,
    render_exit_transition_required_comment,
    render_hello_comment,
    render_rebase_escalation_comment,
    render_rebase_needed_audit_comment,
    render_rebase_pushed_audit_comment,
    render_study_hint_audit_comment,
    render_study_hint_note_body,
    render_transition_comment,
    render_verify_comment,
)


# ---------------------------------------------------------------------------
# Verification (issue #128) — extracted to alice_sm.dispatcher.verify.
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.verify import (  # noqa: E402, F401
    _http_get_body,
    _touches_viewer,
    _verify_enabled,
    default_verifier,
    verify_viewer_route,
)




# ---------------------------------------------------------------------------
# Run report + dependency resolution — extracted to
# alice_sm.dispatcher.report (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.report import (  # noqa: E402, F401
    DependencyResolution,
    RunReport,
    resolve_dependencies,
)


# ---------------------------------------------------------------------------
# Shared handler helpers — extracted to alice_sm.dispatcher.helpers
# (issue #193). The pre-split duplicate definition of
# ``_find_parsed_comment_of_type`` (where the second def shadowed the
# first at runtime) is collapsed to a single definition; behavior is
# unchanged because no caller held a reference to the shadowed first
# definition.
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.helpers import (  # noqa: E402, F401
    _RESEARCH_WORKER_DONE_PREFIX,
    _comment_author_login,
    _current_art_label,
    _find_parsed_comment_of_type,
    _find_resolving_research_note,
    _has_exit_transition_comment,
    _has_prior_study_hint_audit,
    _matches_resolves_issue,
    _research_close_signal,
)




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
    from alice_sm.comments import ReturnToStudy
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
    from alice_sm.comments import parse_dependencies as _parse_deps

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
    spawn_config = SPAWN_MAP.get((ACTIVE_SM_LABEL, art_label))
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


def _process_reviewing(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    close_issue: CloseIssueFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    pr_files: PRFilesFn | None,
    verify_pr: VerifyFn | None,
    post_merge_cleanup: PostMergeCleanupFn | None,
    pr_mergeable: "PRMergeableFn | None" = None,
    attempt_rebase: "Callable[[str], dict[str, Any]] | None" = None,
    spawn_rebase: "Callable[[dict[str, Any], str, str, str], str | None] | None" = None,
    has_live_spawn: "Callable[[int], bool] | None" = None,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda s: None,
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """T2 (reviewing → done) and T3 (reviewing → building) for one issue.

    ``post_merge_cleanup`` (Issue #127) is invoked after a successful
    ``reviewing → done`` transition with the merged PR's head branch and
    the issue number. ``None`` disables cleanup (the test default).

    ``verify_pr`` (Issue #128) is the smoke-test gate run between
    "CI-green" and the actual ``sm:done`` transition. ``None`` disables
    verification entirely (pre-#128 behavior — used by tests that
    don't want to stub the verifier). When non-None, the verifier is
    called with the linked PR number + its changed-file list (obtained
    via ``pr_files``); the verdict's ``outcome`` decides whether to
    proceed, skip-with-audit, or halt at ``sm:reviewing``.

    ``pr_mergeable`` / ``attempt_rebase`` / ``spawn_rebase`` /
    ``has_live_spawn`` (Issue #173) drive the auto-rebase handler on
    unmerged PRs at sm:reviewing. If the PR comes back ``CONFLICTING``,
    the dispatcher fires the three-tier rebase recovery (in-process
    rebase → fresh worker → escalation comment). All four arguments
    default to ``None`` — when any is unset the conflict handler is
    a no-op and the issue stays at sm:reviewing (pre-#173 behavior).
    """
    number = issue["number"]
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to look up PR for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is None:
        # No PR found at all — stay at reviewing. ``find_linked_pr``
        # queries ``--state all``, so this branch only fires when there
        # is genuinely no linked PR (deleted or never existed).
        # Surfaces are escalation-only.
        log(f"[sm-dispatcher] #{number} reviewing but no linked PR found — staying")
        return

    pr_number = pr.get("number")
    if not isinstance(pr_number, int):
        return
    try:
        merge_info = pr_merge_status(repo, pr_number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed merge-status for PR #{pr_number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    if not merge_info.get("merged"):
        # PR still open — check whether it's stuck on a merge conflict
        # and drive the Tier 1/2/3 auto-rebase handler. When the helper
        # callables aren't wired (e.g. tests that don't care about
        # conflicts), this stays a no-op.
        _handle_conflicting_pr(
            issue=issue,
            repo=repo,
            pr_number=pr_number,
            state=state,
            report=report,
            post_comment=post_comment,
            pr_mergeable=pr_mergeable,
            attempt_rebase=attempt_rebase,
            spawn_rebase=spawn_rebase,
            has_live_spawn=has_live_spawn,
            dry_run=dry_run,
            log=log,
            now_iso=now_iso,
        )
        return

    sha = merge_info.get("merge_commit_oid")
    pr_url = merge_info.get("pr_url") or pr.get("url") or "<unknown>"
    if not sha:
        log(f"[sm-dispatcher] #{number} PR merged but no merge_commit_oid — staying")
        return

    try:
        ci = master_ci_status(repo, sha)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed CI lookup for {sha[:8]}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    conclusion = ci.get("conclusion")
    if conclusion is None or conclusion == "pending":
        # No verdict yet — stay at reviewing for next pass.
        return

    if conclusion == "success":
        # ----- Issue #128 verification gate -----
        # CI green is necessary but not sufficient — run an
        # artifact-specific smoke test against the *actually-running*
        # system before declaring the issue done.
        verdict: dict[str, Any] | None = None
        if verify_pr is not None:
            files: list[str] = []
            if pr_files is not None:
                try:
                    files = pr_files(repo, pr_number)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to fetch PR files for "
                        f"#{pr_number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    # Without the file list we can't pick a recipe; bail
                    # this cadence and let the next poll retry. The
                    # issue stays at sm:reviewing.
                    return
            try:
                verdict = verify_pr(pr_number, files)
            except Exception as exc:  # noqa: BLE001 — verifier must never crash the loop
                log(
                    f"[sm-dispatcher] verifier raised for #{number}: "
                    f"{exc.__class__.__name__}: {exc} — treating as verify-failed"
                )
                verdict = {
                    "outcome": "fail",
                    "reason": f"verifier crashed: {exc.__class__.__name__}: {exc}",
                    "route": None,
                }
            outcome = (verdict or {}).get("outcome") or "fail"

            if outcome == "fail":
                v_reason = (verdict or {}).get("reason") or "verification failed"
                v_route = (verdict or {}).get("route")
                # Counter reflects "verifier returned fail this pass" —
                # incremented regardless of whether we actually post a
                # comment (dedup may suppress it). The operator's
                # done-line read of ``verify_failed=N`` should mean
                # "there are still N broken merges parked at reviewing"
                # rather than "we sent N comments to GH this cadence".
                report.verify_failed += 1
                report.verify_records.append((number, "fail", v_reason))
                verify_body = render_verify_comment(
                    "failed",
                    number,
                    reason=v_reason,
                    route=v_route,
                    timestamp=now_iso(),
                )
                if dry_run:
                    log(
                        f"[sm-dispatcher] DRY-RUN would post verify-failed on "
                        f"#{number}: {v_reason}"
                    )
                    return
                if state.has_verify_failed(number):
                    # Already posted this cadence-or-prior; don't spam.
                    # The label stays at sm:reviewing — a human inspects
                    # and either rolls back, escalates, or overrides.
                    log(
                        f"[sm-dispatcher] #{number} verify still failing "
                        f"({v_reason}) — comment already posted, staying"
                    )
                    return
                try:
                    post_comment(repo, number, verify_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to post verify-failed on "
                        f"#{number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    return
                state.mark_verify_failed(number)
                log(
                    f"[sm-dispatcher] #{number} verify-failed posted "
                    f"({v_reason}) — staying at sm:reviewing"
                )
                return

            # outcome == "pass" or "skip" — both allow the transition.
            # Post the audit comment first so the trail records *why*
            # we proceeded (pass means a probe succeeded; skip means
            # no recipe matched). If posting fails we still proceed —
            # the audit is best-effort, not gating.
            v_reason = (verdict or {}).get("reason") or ""
            v_route = (verdict or {}).get("route")
            verify_body = render_verify_comment(
                outcome,
                number,
                reason=v_reason,
                route=v_route,
                timestamp=now_iso(),
            )
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would post verify-{outcome} on "
                    f"#{number}: {v_reason}"
                )
            else:
                try:
                    post_comment(repo, number, verify_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to post verify-{outcome} on "
                        f"#{number}: {exc} — proceeding anyway"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
            if outcome == "pass":
                report.verify_pass += 1
            else:
                report.verify_skip += 1
            report.verify_records.append((number, outcome, v_reason))
            # If the issue had a prior verify-failed entry, clear it —
            # this cadence succeeded and the dedup ledger entry is
            # stale.
            state.clear_verify_failed(number)

        # ----- end verification gate -----

        reason = f"PR merged: {pr_url}, CI green on {sha}"
        body = render_transition_comment(REVIEWING_SM_LABEL, DONE_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"reviewing → done ({sha[:8]})"
            )
            report.transitioned += 1
            report.transitions.append((number, REVIEWING_SM_LABEL, DONE_SM_LABEL))
            return
        try:
            edit_labels(
                repo,
                number,
                add=[DONE_SM_LABEL],
                remove=[REVIEWING_SM_LABEL],
            )
            close_issue(repo, number)
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed close/transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, DONE_SM_LABEL))
        # Issue #173: a successful done transition closes any prior
        # CONFLICTING episode for this issue. Clear the dedup ledger so a
        # future re-entry into sm:reviewing (unlikely, but the state file
        # is long-lived) can fire Tier 1/2/3 again from scratch.
        state.clear_rebase_attempted(number)
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → done (closed)")
        # Issue #127 — restore the worker's working tree to master so the
        # next cycle doesn't read dispatcher.py from this departing
        # worker's feature branch. Cleanup is bounded to this exact
        # transition (merged + green); CI-red and unmerged-closed paths
        # never reach here.
        if post_merge_cleanup is not None:
            try:
                post_merge_cleanup(merge_info.get("head_ref_name"), number)
                report.cleaned_up += 1
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                log(
                    f"[sm-dispatcher] post-merge cleanup raised for #{number}: "
                    f"{exc!r}"
                )
        return

    if conclusion == "failure":
        run_url = ci.get("run_url") or "<unknown>"
        reason = f"CI red on merge: {run_url}"
        body = render_transition_comment(REVIEWING_SM_LABEL, BUILDING_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"reviewing → building (CI red {run_url})"
            )
            report.transitioned += 1
            report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
            return
        try:
            edit_labels(
                repo,
                number,
                add=[BUILDING_SM_LABEL],
                remove=[REVIEWING_SM_LABEL],
            )
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        # CI flipped red — the prior verify-failed entry (if any) was
        # for the green build that just regressed. Clear so when CI
        # eventually re-greens we don't suppress a fresh failure.
        state.clear_verify_failed(number)
        # Issue #173: a CI-red transition also closes the CONFLICTING
        # episode — the work moves back to sm:building and a fresh PR
        # may eventually open. Clear the ledger entry so the next
        # CONFLICTING incident starts fresh.
        state.clear_rebase_attempted(number)
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → building (CI red)")
        return


def _handle_conflicting_pr(
    *,
    issue: dict[str, Any],
    repo: str,
    pr_number: int,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    pr_mergeable: "PRMergeableFn | None",
    attempt_rebase: "Callable[[str], dict[str, Any]] | None",
    spawn_rebase: "Callable[[dict[str, Any], str, str, str], str | None] | None",
    has_live_spawn: "Callable[[int], bool] | None",
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """Issue #173 — Tier 1/2/3 auto-rebase handler for a CONFLICTING PR.

    Called from :func:`_process_reviewing` when the linked PR is still
    open. Looks up the GitHub-computed ``mergeable`` state and, if it
    is ``CONFLICTING``, runs the recovery ladder:

      * **Tier 1 (cheap)** — fire :func:`attempt_rebase`. On success
        post ``[SM] rebase-pushed`` and return; CI will re-fire on the
        new head and the dispatcher picks the PR up next cycle.
      * **Tier 2 (escalation)** — on rebase failure, post
        ``[SM] rebase-needed`` (with the offending file / stderr in the
        reason) AND spawn a fresh worker via :func:`spawn_rebase` to
        resolve conflicts manually. Marks the issue in
        ``state.rebase_attempted`` so a follow-up cycle can detect
        "the spawn died but the PR is still conflicting".
      * **Tier 3 (give up)** — if a prior Tier 2 spawn is dead (no live
        spawn dir) AND the PR is still CONFLICTING, post a
        ``[SM] rebase-escalated`` audit comment exactly once and stop
        retrying. Dedup'd by ``state.rebase_escalated_posted``.

    ``MERGEABLE`` and ``UNKNOWN`` results are no-ops — the existing
    worker self-merge path drives MERGEABLE, and UNKNOWN means GitHub
    is still computing so we wait. Any wiring callable left as ``None``
    short-circuits the handler (test/dry-run escape hatch).
    """
    number = issue["number"]
    if pr_mergeable is None or attempt_rebase is None or spawn_rebase is None:
        # Conflict handler isn't wired this run — silent no-op.
        return

    try:
        info = pr_mergeable(repo, pr_number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed mergeable lookup for PR #{pr_number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    mergeable = info.get("mergeable")
    if mergeable != "CONFLICTING":
        # MERGEABLE → wait for the worker's self-merge.
        # UNKNOWN → GH still computing, retry next cycle.
        # Anything else (None/odd) → treat as UNKNOWN.
        if mergeable in (None, "UNKNOWN"):
            log(
                f"[sm-dispatcher] #{number} PR #{pr_number} mergeable={mergeable!r} "
                f"— retry next cycle"
            )
        return

    branch = info.get("head_ref_name")
    if not branch:
        # Can't act without a branch name. Log and wait.
        log(
            f"[sm-dispatcher] #{number} PR #{pr_number} CONFLICTING but no "
            f"head_ref_name in gh payload — staying"
        )
        return

    # Already escalated to Tier 3 — stay silent until the operator
    # intervenes (either rebases manually, closes the PR, or flips the
    # state ledger entry by transitioning out of sm:reviewing).
    if state.has_rebase_escalated(number):
        log(
            f"[sm-dispatcher] #{number} CONFLICTING + already escalated — staying"
        )
        return

    # Tier 2 spawn already in flight — give it room to work.
    if has_live_spawn is not None and has_live_spawn(number):
        log(
            f"[sm-dispatcher] #{number} CONFLICTING — rebase spawn in flight, waiting"
        )
        return

    # Prior Tier 2 spawn is dead but the PR is still CONFLICTING → Tier 3.
    if state.has_rebase_attempted(number):
        reason = "spawned rebase worker dead but PR still CONFLICTING"
        body = render_rebase_escalation_comment(
            number, branch, reason, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would escalate rebase on "
                f"#{number} (branch={branch})"
            )
            report.rebase_escalated += 1
            report.rebase_records.append((number, "tier3-escalation", reason))
            return
        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] failed to post rebase escalation on "
                f"#{number}: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.mark_rebase_escalated(number)
        report.rebase_escalated += 1
        report.rebase_records.append((number, "tier3-escalation", reason))
        log(
            f"[sm-dispatcher] #{number} rebase escalation surfaced (Tier 3, "
            f"branch={branch})"
        )
        return

    # Tier 1 — cheap in-process rebase attempt.
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would attempt rebase on "
            f"#{number} (branch={branch})"
        )
        return

    result = attempt_rebase(branch)
    if result.get("ok"):
        report.rebase_pushed += 1
        reason = result.get("reason") or "rebased and pushed"
        report.rebase_records.append((number, "tier1-pushed", reason))
        body = render_rebase_pushed_audit_comment(
            number, branch, timestamp=now_iso()
        )
        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] rebase pushed on #{number} but audit "
                f"comment failed: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            # Non-fatal: the push already happened.
        log(
            f"[sm-dispatcher] #{number} auto-rebased and pushed branch={branch}"
        )
        return

    # Tier 2 — rebase failed. Post audit + spawn worker.
    reason = result.get("reason") or "auto-rebase failed"
    audit_body = render_rebase_needed_audit_comment(
        number, branch, reason, timestamp=now_iso()
    )
    try:
        post_comment(repo, number, audit_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post rebase-needed audit on "
            f"#{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    try:
        spawn_id = spawn_rebase(issue, repo, branch, reason)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to launch rebase spawn for "
            f"#{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        # Popen / filesystem errors: log + continue; the audit comment
        # was already posted so the cadence trail records the attempt.
        log(
            f"[sm-dispatcher] rebase spawn launch raised OSError on "
            f"#{number}: {exc}"
        )
        return

    if spawn_id is None:
        log(
            f"[sm-dispatcher] rebase spawn returned None for #{number} — "
            f"will retry next cycle"
        )
        return

    state.mark_rebase_attempted(number)
    report.rebase_spawned += 1
    report.rebase_records.append((number, "tier2-spawn", reason))
    log(
        f"[sm-dispatcher] #{number} rebase spawn launched ({spawn_id}, "
        f"branch={branch})"
    )


# ---------------------------------------------------------------------------
# Issue #157 — sm:needs_study handler
# ---------------------------------------------------------------------------


def _process_draft(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    art_whitelist: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:draft → sm:needs_study on a trusted ``[SM] route-to-study`` comment.

    The ``art=<art-label>`` field is optional. When present *and*
    different from the issue's current ``art:*`` label, the dispatcher
    swaps the label atomically with the state transition.
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

    from alice_sm.comments import RouteToStudy

    parsed = _find_parsed_comment_of_type(
        comments,
        RouteToStudy,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
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
    report.transitioned += 1
    report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"draft → needs_study ({reason})"
    )


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
         via :func:`alice_sm.comments.parse_comment`. The first parsed
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
    # Local import to avoid a top-of-module cycle: ``alice_sm.comments``
    # imports ``ART_LABEL_WHITELIST`` / ``TRUSTED_AUTHORS`` from this
    # module.
    from alice_sm.comments import (
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


# ---------------------------------------------------------------------------
# Issue #164 — sm:designing / design_review / designed / compacting / building
# ---------------------------------------------------------------------------


def _process_designing(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:designing → sm:design_review on a fresh ``[SM] design-ready`` comment.

    The thinking-agent is running and producing a design draft. When it
    emits ``[SM] design-ready note=[[...]]`` the dispatcher relabels the
    issue ``sm:design_review`` and posts a ``[SM] design-ready-audit``
    so Speaking's review loop knows to pick it up.

    No design-ready comment yet → no action; the agent is still
    working. The handler is otherwise idempotent: once the label flips
    to ``sm:design_review`` the issue's next pass goes through
    :func:`_process_design_review` instead.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import DesignReady

    parsed = _find_parsed_comment_of_type(
        comments,
        DesignReady,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"no [SM] design-ready comment yet"
        )
        return

    reason = f"design-ready note=[[{parsed.note}]]"
    transition_body = render_transition_comment(
        DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL, reason
    )
    audit_body = render_design_ready_audit_comment(
        number, parsed.note, timestamp=now_iso()
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"designing → design_review ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[DESIGN_REVIEW_SM_LABEL],
            remove=[DESIGNING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
        post_comment(repo, number, audit_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"failed to transition to design_review: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designing → design_review ({reason})"
    )


def _process_design_review(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:design_review → sm:designed | sm:designing | sm:rejected.

    Speaking owns this gate. Two parseable verbs from a trusted author:

      * ``[SM] design-approved`` → ``sm:designed``. Clears the per-issue
        revision counter so a future re-entry starts fresh.
      * ``[SM] design-revise reason=... feedback=[[...]]`` → bumps
        :attr:`DispatcherState.design_revisions` for the issue. While
        the count is at or below :data:`DESIGN_REVISION_CAP` the issue
        bounces back to ``sm:designing`` for another iteration.
        On the (cap+1)th bounce the issue is routed to ``sm:rejected``
        with a ``[SM] design-revisions-capped`` audit so the operator
        sees why the loop terminated.

    Comments that aren't ``[SM] design-{approved,revise}`` are
    ignored; we wait for the next pass.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import DesignApproved, DesignRevise

    parsed = _find_parsed_comment_of_type(
        comments,
        (DesignApproved, DesignRevise),
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"awaiting design-approved / design-revise"
        )
        return

    if isinstance(parsed, DesignApproved):
        target = DESIGNED_SM_LABEL
        reason = "design-approved"
        transition_body = render_transition_comment(
            DESIGN_REVIEW_SM_LABEL, target, reason
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"design_review → designed (approved)"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, DESIGN_REVIEW_SM_LABEL, target)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[target],
                remove=[DESIGN_REVIEW_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to transition to designed: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_design_revisions(number)
        report.transitioned += 1
        report.transitions.append((number, DESIGN_REVIEW_SM_LABEL, target))
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"design_review → designed (approved)"
        )
        return

    # ----- design-revise branch -----
    # Use the pre-existing count to decide: if the count is already at
    # the cap, the new revise comment is the (cap+1)th bounce — reject.
    # Otherwise increment and bounce back to designing.
    prior = state.design_revision_count(number)
    if prior >= DESIGN_REVISION_CAP:
        capped_count = prior + 1
        reason = (
            f"design-revisions-capped count={capped_count} "
            f"cap={DESIGN_REVISION_CAP}"
        )
        transition_body = render_transition_comment(
            DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL, reason
        )
        audit_body = render_design_revisions_capped_comment(
            number, capped_count, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"design_review → rejected ({reason})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[REJECTED_SM_LABEL],
                remove=[DESIGN_REVIEW_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
            post_comment(repo, number, audit_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to transition to rejected: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_design_revisions(number)
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL)
        )
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"design_review → rejected ({reason})"
        )
        return

    # Under the cap → iterate.
    new_count = state.bump_design_revisions(number)
    reason = (
        f'design-revise iteration={new_count} '
        f'reason="{parsed.reason}" feedback=[[{parsed.feedback}]]'
    )
    transition_body = render_transition_comment(
        DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL, reason
    )
    if dry_run:
        # Roll back the bump so dry-run is side-effect-free on the
        # ledger; we already incremented above to render the reason.
        state.design_revisions[number] = new_count - 1
        if state.design_revisions[number] == 0:
            state.clear_design_revisions(number)
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"design_review → designing ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[DESIGNING_SM_LABEL],
            remove=[DESIGN_REVIEW_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        # Undo the ledger bump — the GH side didn't move, so the next
        # pass should observe the same revise comment and retry.
        state.design_revisions[number] = new_count - 1
        if state.design_revisions[number] == 0:
            state.clear_design_revisions(number)
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"failed to bounce to designing: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"design_review → designing ({reason})"
    )


def _process_designed(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    live_spawn_dir: Callable[[int], pathlib.Path | None] | None,
    dry_run: bool,
    log: Callable[[str], None],
    has_live_speaking_spawn: Callable[[int], bool] | None = None,
    count_running_speaking: Callable[[], int] | None = None,
    spawn_speaking: Callable[[dict[str, Any], str, str], str | None] | None = None,
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

    spawn_config = SPAWN_MAP.get((DESIGNED_SM_LABEL, art_label))
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
    has_live_speaking_spawn: Callable[[int], bool] | None,
    count_running_speaking: Callable[[], int] | None,
    spawn_speaking: Callable[[dict[str, Any], str, str], str | None] | None,
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

    # Spawn first — the speaking-agent posts its own
    # [SM] speaking-spawn-started audit comment before launching the
    # shim, so failure to spawn leaves a recoverable audit trail and
    # doesn't move the label.
    try:
        spawn_id = spawn_speaking(issue, art_label, repo)
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


def _process_compacting(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    has_live_spawn: Callable[[int], bool] | None,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:compacting → sm:building on the agent's ``[SM] build-started`` comment.

    The thinking-agent is mid-compaction (container restart in
    progress). When it comes back up in BUILD mode it posts
    ``[SM] build-started`` — that's the dispatcher's signal to flip
    the label so :func:`_process_building` takes over and watches for
    the PR.

    The ``has_live_spawn`` callable is consulted as a confidence
    check: if the agent died during compaction (no live spawn) we
    still honor the build-started signal but log a warning, since the
    audit trail says the agent claimed it started; humans can sort it
    out from there.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import BuildStarted

    parsed = _find_parsed_comment_of_type(
        comments,
        BuildStarted,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"awaiting [SM] build-started"
        )
        return

    if has_live_spawn is not None and not has_live_spawn(number):
        log(
            f"[sm-dispatcher] compacting #{number}: WARNING — "
            f"build-started seen but no live spawn dir; agent may have "
            f"died during compaction. Transitioning anyway per audit trail."
        )

    reason = "build-started"
    transition_body = render_transition_comment(
        COMPACTING_SM_LABEL, BUILDING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"compacting → building (build-started)"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, COMPACTING_SM_LABEL, BUILDING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[BUILDING_SM_LABEL],
            remove=[COMPACTING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"failed to transition to building: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, COMPACTING_SM_LABEL, BUILDING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"compacting → building (build-started)"
    )


def _process_building(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:building → sm:reviewing once a linked PR appears.

    Mirrors the T1 sub-path inside :func:`_process_selected`: an
    open linked PR is the "build complete" signal. The build-phase
    agent opens its PR as a draft (per ``per-issue-build.md``); the
    dispatcher relabels and hands off to the existing reviewing-state
    pipeline (CI + verify + Sonnet review).
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


def _process_stale_closed(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """Phase 1.6 sweep: route a closed issue with a non-terminal ``sm:*``
    label to its correct terminal state.

    The issue is already closed — we never re-open and we never close
    further; only labels and the ``[SM] transition`` audit comment are
    written. Decision tree:

      * linked PR merged + master CI green → ``sm:done``
      * linked PR merged + master CI red   → ``sm:rejected``
        (the merge happened but broke master; the work shipped-but-bad
        and downstream tracking should treat it as rejected pending
        follow-up.)
      * linked PR closed-unmerged          → ``sm:rejected``
      * no linked PR at all                → ``sm:rejected``
        (manual close or supersession — there's no merge artifact, so
        the safe terminal state is rejected.)

    A pending master CI verdict is treated as "wait" — we stay at the
    stale label and let the next pass re-evaluate. This keeps the
    sweep idempotent under flaky CI: we'd rather leave a stale label
    one more cadence than commit to ``sm:done`` before the build is
    actually green.
    """
    number = issue["number"]
    stale_label = _current_sm_label(issue)
    if stale_label is None:
        # Defensive: the helper already filters to non-terminal sm:*,
        # but if some odd label set sneaks through (multi-sm, typo),
        # don't guess.
        names = _label_names(issue)
        sm_labels_seen = [n for n in names if n.startswith("sm:")]
        log(
            f"[sm-dispatcher] sweep skip #{number}: "
            f"ambiguous sm:* label set {sm_labels_seen!r}"
        )
        return
    if stale_label in TERMINAL_SM_LABELS:
        # Belt-and-suspenders: helper's client-side filter should have
        # excluded this. If we got here anyway, do nothing.
        return

    # Resolve linked PR + outcome.
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] sweep: failed PR lookup for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    target_label: str
    reason: str
    if pr is None:
        # Closed with no PR linkage: manual close, supersession, or
        # a bot that closed without a "Closes #" reference. Without a
        # merge artifact the safe terminal is rejected.
        target_label = REJECTED_SM_LABEL
        reason = "issue closed without linked PR (manual close or supersession)"
    else:
        pr_number = pr.get("number")
        pr_state = (pr.get("state") or "").upper()
        if not isinstance(pr_number, int):
            log(
                f"[sm-dispatcher] sweep skip #{number}: "
                f"linked PR payload missing number ({pr!r})"
            )
            return
        if pr_state == "MERGED":
            try:
                merge_info = pr_merge_status(repo, pr_number)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] sweep: merge-status failed for "
                    f"PR #{pr_number}: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            sha = merge_info.get("merge_commit_oid")
            pr_url = merge_info.get("pr_url") or pr.get("url") or "<unknown>"
            if not sha:
                log(
                    f"[sm-dispatcher] sweep skip #{number}: "
                    f"PR #{pr_number} reports MERGED but no merge_commit_oid"
                )
                return
            try:
                ci = master_ci_status(repo, sha)
            except GHCommandError as exc:
                log(f"[sm-dispatcher] sweep: CI lookup failed for {sha[:8]}: {exc}")
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            conclusion = ci.get("conclusion")
            if conclusion is None or conclusion == "pending":
                # Hold the stale label one more cadence rather than
                # commit to a terminal before CI returns a verdict.
                log(
                    f"[sm-dispatcher] sweep wait #{number}: "
                    f"PR #{pr_number} merged but master CI is {conclusion!r}"
                )
                return
            if conclusion == "success":
                target_label = DONE_SM_LABEL
                reason = (
                    f"closed-by-merge sweep: PR #{pr_number} merged at {sha}, "
                    f"master CI success ({pr_url})"
                )
            else:
                # CI red post-merge: the work shipped but broke master.
                # Downgrade to rejected so a human picks up the follow-up;
                # we don't have the Phase 2 quality-gate plumbing yet.
                run_url = ci.get("run_url") or "<unknown>"
                target_label = REJECTED_SM_LABEL
                reason = (
                    f"closed-by-merge sweep: PR #{pr_number} merged at {sha} "
                    f"but master CI failure ({run_url})"
                )
        elif pr_state == "CLOSED":
            target_label = REJECTED_SM_LABEL
            reason = f"PR #{pr_number} closed without merge"
        else:
            # PR is still OPEN (or some state we don't recognise) and
            # the issue is closed. Possible scenarios: the PR was
            # un-merged after the fact, or the issue was hand-closed
            # while a PR still exists. Either way, don't sweep — let a
            # human (or a later phase) decide.
            log(
                f"[sm-dispatcher] sweep skip #{number}: "
                f"issue closed but linked PR #{pr_number} is {pr_state!r}"
            )
            return

    body = render_transition_comment(stale_label, target_label, reason)
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would sweep #{number}: "
            f"{stale_label} → {target_label} ({reason})"
        )
        report.swept += 1
        report.transitions.append((number, stale_label, target_label))
        return
    try:
        edit_labels(
            repo,
            number,
            add=[target_label],
            remove=[stale_label],
        )
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] sweep failed to transition #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.swept += 1
    report.transitions.append((number, stale_label, target_label))
    log(
        f"[sm-dispatcher] swept #{number}: "
        f"{stale_label} → {target_label} (issue stays closed)"
    )


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
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    art_whitelist=ART_LABEL_WHITELIST,
                    dry_run=dry_run,
                    log=log,
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


if __name__ == "__main__":
    sys.exit(main())
