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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Defaults + constants
# ---------------------------------------------------------------------------

DEFAULT_REPO = "jcronq/alice"
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
DEFAULT_STATE_FILE = "sm-dispatcher-state.json"

# Cap on the dedup list. Issue numbers are monotonic, so dropping the
# oldest first is safe — once an issue is closed and "seen," it stays
# closed; we don't need an unbounded ledger.
SEEN_ISSUE_CAP = 1000

# Pull recent open ``sm:*`` issues per poll. Bounded by the active task
# slate, not historical issues.
RECENT_ISSUE_LIMIT = 50

# v0 author whitelist. Bot identities (the eventual ``alice-bot`` GitHub
# App) land in a later phase — until then only Jason can drop tasks into
# Alice's lane.
TRUSTED_AUTHORS: frozenset[str] = frozenset({"jcronq"})

# Strict ``sm:*`` allow-list. A typo like ``sm:building-pleaserun`` must
# be skipped rather than fuzzy-matched into ``sm:building`` — drift in
# the state vocabulary corrupts the whole protocol.
SM_LABEL_WHITELIST: frozenset[str] = frozenset(
    {
        "sm:draft",
        "sm:selected",
        "sm:building",
        "sm:reviewing",
        "sm:validating",
        "sm:done",
        "sm:rejected",
        "sm:blocked",
    }
)

# Strict ``art:*`` allow-list. Every task must declare what kind of
# artifact it produces; the dispatcher refuses to engage with tasks that
# don't.
ART_LABEL_WHITELIST: frozenset[str] = frozenset(
    {
        "art:code",
        "art:research_note",
        "art:experiment",
        "art:config_change",
    }
)

# v0 only acted on ``sm:selected``. Phase 1.5 also acts on
# ``sm:reviewing``. Other ``sm:*`` states will be handled in later
# phases (building → spawn agent, validating → quality-gate, etc.).
ACTIVE_SM_LABEL = "sm:selected"
REVIEWING_SM_LABEL = "sm:reviewing"
BUILDING_SM_LABEL = "sm:building"
DONE_SM_LABEL = "sm:done"
REJECTED_SM_LABEL = "sm:rejected"

# Terminal ``sm:*`` states — the dispatcher's sweep pass leaves these
# alone. Non-terminal labels on a *closed* issue indicate a missed
# transition (Phase 1.6 sweep target).
TERMINAL_SM_LABELS: frozenset[str] = frozenset({DONE_SM_LABEL, REJECTED_SM_LABEL})
NON_TERMINAL_SM_LABELS: frozenset[str] = SM_LABEL_WHITELIST - TERMINAL_SM_LABELS

# Schema version of the state file. Bump if the structure changes
# incompatibly.
STATE_VERSION = 1


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Phase 2 — agent spawn constants
# ---------------------------------------------------------------------------

# Per-(state, artifact) spawn config. Each entry tells the dispatcher
# how to frame the spawn prompt for one ``(sm:*, art:*)`` combination:
#
#   * ``system_prompt_role`` — short role label rendered into the
#     prompt header (Claude doesn't actually get a separate system
#     prompt from us; the CLI's defaults take over). The label drives
#     the in-prompt persona framing.
#   * ``instruction_trailer`` — final instructions appended after the
#     issue body. ``{issue_number}`` is the substitution token.
#   * ``system_prompt_module`` (optional) — dotted path to a system
#     prompt constant when the agent is a structured-output sub-agent
#     (e.g., the code reviewer). The future
#     ``(sm:reviewing, *)`` integration consumes this; v1
#     ``(sm:selected, *)`` workers ignore it and rely on the claude
#     CLI's default system prompt.
#
# Out of scope for v1: the (persona × runtime) matrix from the design
# doc. v1 spawns Claude for every ``(sm:selected, art:*)`` row; the
# spawn map is the extension point for later phases. Issue #107 adds
# the first ``(sm:reviewing, art:code)`` row — the dispatcher path
# that consumes it is wired separately.
SPAWN_MAP: dict[tuple[str, str], dict[str, str]] = {
    ("sm:selected", "art:code"): {
        "system_prompt_role": "code-worker",
        "instruction_trailer": (
            "Open a PR titled appropriately with `Closes #{issue_number}` "
            "in the body. Self-merge once CI is green. Do not --no-verify."
        ),
    },
    ("sm:selected", "art:config_change"): {
        "system_prompt_role": "code-worker",
        "instruction_trailer": (
            "Open a PR titled appropriately with `Closes #{issue_number}` "
            "in the body. Self-merge once CI is green. Do not --no-verify."
        ),
    },
    ("sm:selected", "art:research_note"): {
        "system_prompt_role": "research-writer",
        "instruction_trailer": (
            "Produce a research note at "
            "~/alice-mind/cortex-memory/research/<date>-<slug>.md. After "
            "writing the note, edit issue #{issue_number} to relabel "
            "sm:selected → sm:done and post a "
            "`[SM] transition from=selected to=done reason=\"research "
            "note at <path>\"` comment."
        ),
    },
    ("sm:selected", "art:experiment"): {
        "system_prompt_role": "research-writer",
        "instruction_trailer": (
            "Same as research_note for v1. Produce a note with "
            "hypothesis/null/verdict frontmatter; transition to done "
            "when complete."
        ),
    },
    # Issue #107 — code-quality reviewer for PRs at sm:reviewing. The
    # ``system_prompt_module`` is the dotted import path to
    # :data:`alice_speaking.review.code_reviewer.CODE_REVIEWER_SYSTEM_PROMPT`;
    # the dispatcher's future ``(sm:reviewing, art:code)`` path will
    # load it and drive a Sonnet sub-agent that returns the structured
    # JSON verdict defined in that module. v1 dispatcher does NOT yet
    # consume this entry from ``_process_reviewing`` — it sits here as
    # the integration point for the follow-up wiring.
    ("sm:reviewing", "art:code"): {
        "system_prompt_role": "code-reviewer",
        "system_prompt_module": (
            "alice_speaking.review.code_reviewer:CODE_REVIEWER_SYSTEM_PROMPT"
        ),
        "instruction_trailer": (
            "Review the PR linked from issue #{issue_number}. Return a "
            "single STRICT JSON object matching the schema in your system "
            "prompt — no markdown fences, no prose. ``verdict: approved`` "
            "means the dispatcher will close the issue at "
            "sm:reviewing → sm:done; ``verdict: needs_revision`` means "
            "sm:reviewing → sm:building."
        ),
    },
}

# Cap on simultaneously running claude subprocess spawns. Excess
# eligible ``sm:selected`` issues stay queued until the next dispatcher
# pass — back-pressure rather than crash-on-overload.
MAX_CONCURRENT_SPAWNS = 2

# Per-spawn workdir. One subdir per spawn id, with ``prompt.txt``,
# ``pidfile``, ``stdout.log``, ``stderr.log``. Dead spawns get moved
# under ``.finished/<id>/`` by :func:`count_running_spawns` so the live
# count stays accurate on the next pass.
SPAWN_DIR = pathlib.Path("/state/worker/sm-dispatcher-spawns")

# The ``claude`` binary used to launch worker agents. Issue #101's
# original spec named ``/opt/alice-venv/bin/claude`` but the live host
# ships ``/usr/bin/claude``. We resolve at run time: prefer the spec'd
# path if it exists, fall back to the on-PATH binary.
CLAUDE_BIN_PREFERRED = "/opt/alice-venv/bin/claude"
CLAUDE_BIN_FALLBACK = "claude"

# Prefix on the audit-trail comment that signals "we've already spawned
# an agent on this issue". The next pass's
# :func:`gh_find_unspawned_selected_issues` filters on this prefix +
# trusted-author authorship to dedup.
SPAWN_STARTED_PREFIX = "[SM] spawn-started"


# ---------------------------------------------------------------------------
# Issue #128 — sm:reviewing → sm:done verification (smoke-test) gate
# ---------------------------------------------------------------------------
#
# After CI is green on the merge commit but before we relabel the issue
# sm:done, run an artifact-specific smoke test. CI catches regressions
# inside the source tree; this third tier confirms the actually-running
# system reflects the change (the canonical motivating bug: PR #119
# merged green, but the live viewer process was still serving stale
# Python — only a real HTTP probe would have caught it).
#
# v1 (this issue's minimal cut) only ships the *viewer-route* recipe:
# if the merged PR touched any file under ``src/alice_viewer/``, the
# dispatcher GETs a configured URL on the running viewer and asserts
# a marker substring is present in the response body. Anything else
# (dispatcher touches, speaking touches, art:research_note, ...) is
# treated as ``verify-skip`` — recorded in the audit comment but
# allowed through to ``sm:done``. The shape extends to more recipes
# without changing the dispatcher control flow.

VERIFY_PASS_PREFIX = "[SM] verify-pass"
VERIFY_SKIP_PREFIX = "[SM] verify-skip"
VERIFY_FAILED_PREFIX = "[SM] verify-failed"

# Path prefix that flags a PR file as a "viewer touch" — anything under
# the alice_viewer package, including templates and static assets. Kept
# narrow so dispatcher / speaking / sm changes don't accidentally
# trigger the viewer probe.
VERIFY_VIEWER_PATH_PREFIX = "src/alice_viewer/"

# Default URL hit by the viewer-route smoke test. Override with
# ``ALICE_VERIFY_VIEWER_URL``. The path is intentionally the index —
# v1 only needs to confirm the FastAPI process is alive and serving
# *some* response with the marker; per-route assertions are a follow-up.
VERIFY_VIEWER_URL_ENV = "ALICE_VERIFY_VIEWER_URL"
VERIFY_VIEWER_URL_DEFAULT = "http://localhost:7777/"

# Substring asserted in the viewer response body. Override with
# ``ALICE_VERIFY_VIEWER_MARKER``. ``</html>`` is the cheapest
# "rendered a template, didn't 500" signal short of parsing HTML.
VERIFY_VIEWER_MARKER_ENV = "ALICE_VERIFY_VIEWER_MARKER"
VERIFY_VIEWER_MARKER_DEFAULT = "</html>"

# HTTP timeout for the viewer probe. Short so a wedged viewer doesn't
# stall the whole dispatcher pass.
VERIFY_HTTP_TIMEOUT_SECONDS = 5

# Master kill-switch. ``ALICE_VERIFY_ENABLED=0`` reverts the dispatcher
# to pre-#128 behavior — straight from CI-green to ``sm:done`` with no
# smoke test. Defaults to enabled.
VERIFY_ENABLED_ENV = "ALICE_VERIFY_ENABLED"

# Shared working tree on the worker — all v1 workers checkout into this
# one repo. Issue #127: after a PR merges, the dispatcher restores this
# tree to ``BASE_BRANCH`` so the next cycle reads master and not the
# departing worker's feature branch. If we move to per-worker worktrees
# in a later phase, cleanup migrates with the worker's lifecycle and
# this constant becomes per-spawn config.
WORKER_REPO_PATH = pathlib.Path("/home/alice/alice")
BASE_BRANCH = "master"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GHCommandError(RuntimeError):
    """Raised when a ``gh`` invocation exits non-zero.

    Mirrors :class:`alice_watchers.github.GHCommandError` — we keep the
    stderr around so the auth-failure / rate-limit heuristic has
    something to sniff.
    """

    def __init__(self, returncode: int, stderr: str, args: list[str]) -> None:
        super().__init__(f"gh exited {returncode}: {stderr.strip()[:400]}")
        self.returncode = returncode
        self.stderr = stderr
        self.args = args

    @property
    def looks_like_auth_failure(self) -> bool:
        msg = self.stderr.lower()
        return any(
            needle in msg
            for needle in (
                "401",
                "403",
                "bad credentials",
                "requires authentication",
                "must authenticate",
                "auth login",
            )
        )

    @property
    def looks_like_rate_limit(self) -> bool:
        msg = self.stderr.lower()
        return any(
            needle in msg
            for needle in (
                "rate limit",
                "secondary rate limit",
                "api rate limit exceeded",
            )
        )


# ---------------------------------------------------------------------------
# State load/save
# ---------------------------------------------------------------------------


@dataclass
class DispatcherState:
    """In-memory view of the dispatcher's persisted state.

    ``hello_commented`` is the FIFO list of issue numbers we've already
    posted the dispatcher-hello on. Insertion-ordered so the oldest
    fall off first when we hit :data:`SEEN_ISSUE_CAP`.

    ``verify_failed_posted`` (issue #128) is the FIFO list of issue
    numbers we've already posted a ``[SM] verify-failed`` comment on
    while the issue remained at ``sm:reviewing``. Without this dedup,
    every dispatcher cadence would re-post the failure (CI stays green
    on the same merge commit, so the verifier keeps running). Cleared
    implicitly when the issue eventually transitions out of reviewing.
    """

    version: int = STATE_VERSION
    hello_commented: list[int] = field(default_factory=list)
    verify_failed_posted: list[int] = field(default_factory=list)

    def has_hello(self, number: int) -> bool:
        return number in self.hello_commented

    def mark_hello(self, number: int) -> None:
        # Move-to-front semantics would defeat FIFO eviction. Append-only.
        if number in self.hello_commented:
            return
        self.hello_commented.append(number)
        # Hard cap — drop oldest first.
        if len(self.hello_commented) > SEEN_ISSUE_CAP:
            overflow = len(self.hello_commented) - SEEN_ISSUE_CAP
            del self.hello_commented[:overflow]

    def has_verify_failed(self, number: int) -> bool:
        return number in self.verify_failed_posted

    def mark_verify_failed(self, number: int) -> None:
        if number in self.verify_failed_posted:
            return
        self.verify_failed_posted.append(number)
        if len(self.verify_failed_posted) > SEEN_ISSUE_CAP:
            overflow = len(self.verify_failed_posted) - SEEN_ISSUE_CAP
            del self.verify_failed_posted[:overflow]

    def clear_verify_failed(self, number: int) -> None:
        # Called when the issue transitions out of sm:reviewing — the
        # dedup signal is scoped to "still pending verification on this
        # merge". Once the label changes (to done after a retry pass,
        # or to building if CI flips red), the ledger entry is stale.
        try:
            self.verify_failed_posted.remove(number)
        except ValueError:
            pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hello_commented": list(self.hello_commented),
            "verify_failed_posted": list(self.verify_failed_posted),
        }


def load_state(state_path: pathlib.Path) -> DispatcherState:
    """Load dispatcher state. Returns an empty skeleton on first run."""
    if not state_path.is_file():
        return DispatcherState()
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        # Corrupt state: log via stderr and start fresh. Re-firing the
        # dispatcher-hello on existing ``sm:selected`` issues once is
        # acceptable; staying broken isn't.
        print(
            f"[sm-dispatcher] state at {state_path} is corrupt — resetting",
            file=sys.stderr,
        )
        return DispatcherState()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return DispatcherState()
    raw = data.get("hello_commented") or []
    numbers: list[int] = [int(n) for n in raw if isinstance(n, int)]
    # ``verify_failed_posted`` was added in #128. Older state files
    # don't have the field; default to empty so the dispatcher keeps
    # working across the upgrade without a manual reset.
    raw_vf = data.get("verify_failed_posted") or []
    vf_numbers: list[int] = [int(n) for n in raw_vf if isinstance(n, int)]
    return DispatcherState(
        version=STATE_VERSION,
        hello_commented=numbers,
        verify_failed_posted=vf_numbers,
    )


def save_state(state_path: pathlib.Path, state: DispatcherState) -> None:
    """Atomically replace the state file. Caps the seen-issue list."""
    if len(state.hello_commented) > SEEN_ISSUE_CAP:
        overflow = len(state.hello_commented) - SEEN_ISSUE_CAP
        del state.hello_commented[:overflow]
    if len(state.verify_failed_posted) > SEEN_ISSUE_CAP:
        overflow = len(state.verify_failed_posted) - SEEN_ISSUE_CAP
        del state.verify_failed_posted[:overflow]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=state_path.parent, prefix=".sm-dispatcher-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, state_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# gh CLI shims (injectable for tests)
# ---------------------------------------------------------------------------


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
    stdout = _run_gh(args)
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
        "label:sm:draft,sm:selected,sm:building,sm:reviewing,sm:validating",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _run_gh(args)
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
    stdout = _run_gh(args)
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
    _run_gh(args)


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
    _run_gh(args)


def gh_close_issue(repo: str, number: int, *, gh_bin: str = "gh") -> None:
    """Close an issue via ``gh issue close``."""
    args = [gh_bin, "issue", "close", str(number), "--repo", repo]
    _run_gh(args)


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
    stdout = _run_gh(args)
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
    stdout = _run_gh(args)
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
    stdout = _run_gh(args)
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
    stdout = _run_gh(args)
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
    ``src/alice_viewer/`` flips the recipe on). An empty list on a
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
    stdout = _run_gh(args)
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


# Callable aliases — tests inject fakes here without monkeypatching the
# module-level names.
ListIssuesFn = Callable[[str], list[dict[str, Any]]]
PostCommentFn = Callable[[str, int, str], None]
EditLabelsFn = Callable[..., None]
CloseIssueFn = Callable[[str, int], None]
FindLinkedPRFn = Callable[[str, int], dict[str, Any] | None]
PRMergeStatusFn = Callable[[str, int], dict[str, Any]]
MasterCIStatusFn = Callable[[str, str], dict[str, Any]]
ListCommentsFn = Callable[[str, int], list[dict[str, Any]]]
FindUnspawnedFn = Callable[[str], list[dict[str, Any]]]
PRFilesFn = Callable[[str, int], list[str]]
# Verifier contract: takes a PR number + the list of files it changed,
# returns a verdict dict ``{outcome, reason, route}``. ``outcome`` is
# one of ``"pass"`` / ``"skip"`` / ``"fail"`` — corresponding to the
# three audit comment shapes. ``route`` is the URL hit on pass / fail
# (None on skip).
VerifyFn = Callable[[int, list[str]], dict[str, Any]]
# (cmd_args, cwd) → CompletedProcess. ``cmd_args`` is the trailing
# argv (no leading ``git``); ``cwd`` is the repo to operate in. Tests
# inject a fake to avoid touching the real working tree.
GitRunFn = Callable[[list[str], pathlib.Path], "subprocess.CompletedProcess[str]"]
PostMergeCleanupFn = Callable[[str | None, int], None]


# ---------------------------------------------------------------------------
# Issue #127 — post-merge working-tree cleanup
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: pathlib.Path,
    *,
    timeout: int = 30,
) -> "subprocess.CompletedProcess[str]":
    """Invoke ``git -C <cwd> <args...>`` and return the CompletedProcess.

    Never raises on non-zero exit — callers inspect ``returncode`` /
    ``stderr`` and decide whether to log+continue or bail. Wraps
    ``OSError`` / ``TimeoutExpired`` as a synthetic returncode=-1 result
    so the cleanup helper has a uniform shape to inspect.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(cwd), *args],
            returncode=-1,
            stdout="",
            stderr=str(exc),
        )


def _post_merge_cleanup(
    *,
    repo_path: pathlib.Path,
    branch: str | None,
    issue_number: int,
    base_branch: str = BASE_BRANCH,
    run_git: GitRunFn = _run_git,
    log: Callable[[str], None],
) -> None:
    """Restore the worker's shared tree to ``base_branch`` after a PR merge.

    Issue #127. Called from the ``sm:reviewing → sm:done`` transition
    (i.e., only after the dispatcher has confirmed PR merged + master CI
    green). Idempotent; safe to call when already on master or when the
    feature branch has already been pulled.

    Steps (each tolerates the "already in target state" case):
      1. If the working tree has uncommitted changes — log a warning
         and skip the rest. We never want to clobber in-flight edits;
         the operator handles it manually.
      2. ``git checkout base_branch`` (skipped if already on it).
      3. ``git pull --ff-only origin base_branch``. Failure here is
         logged but non-fatal — the checkout still succeeded.
      4. ``git branch -d <branch>`` for the merged feature branch.
         Skipped if ``branch`` is None, equal to ``base_branch``, or
         already absent locally.

    All log lines use the ``[SM] checkout`` prefix for the audit trail.
    """
    log_prefix = f"[SM] checkout #{issue_number}"

    if not repo_path.is_dir():
        log(f"{log_prefix} skip: repo path missing at {repo_path}")
        return

    dirty = run_git(["status", "--porcelain"], repo_path)
    if dirty.returncode != 0:
        # If we can't tell, be defensive — don't touch the tree.
        log(
            f"{log_prefix} skip: git status failed in {repo_path} "
            f"({dirty.stderr.strip() or dirty.returncode}); leaving alone"
        )
        return
    if dirty.stdout.strip():
        log(
            f"{log_prefix} skip: uncommitted changes in {repo_path} "
            f"(branch={branch!r}); not switching — operator should resolve"
        )
        return

    current = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    current_branch = current.stdout.strip() if current.returncode == 0 else None
    if current.returncode != 0:
        log(
            f"{log_prefix}: could not read current branch "
            f"({current.stderr.strip() or current.returncode}); continuing"
        )

    if current_branch == base_branch:
        log(f"{log_prefix}: {repo_path} already on {base_branch}")
    else:
        checkout = run_git(["checkout", base_branch], repo_path)
        if checkout.returncode != 0:
            log(
                f"{log_prefix} failed: git checkout {base_branch} "
                f"({checkout.stderr.strip() or checkout.returncode}); "
                f"leaving tree on {current_branch!r}"
            )
            return
        log(
            f"{log_prefix}: switched {repo_path} from "
            f"{current_branch!r} to {base_branch}"
        )

    pull = run_git(["pull", "--ff-only", "origin", base_branch], repo_path)
    if pull.returncode != 0:
        log(
            f"{log_prefix}: git pull --ff-only origin {base_branch} failed "
            f"({pull.stderr.strip() or pull.returncode}); next cycle will retry"
        )
    else:
        log(f"{log_prefix}: pulled origin/{base_branch} into {repo_path}")

    if branch and branch != base_branch:
        delete = run_git(["branch", "-d", branch], repo_path)
        if delete.returncode == 0:
            log(f"{log_prefix}: deleted local branch {branch!r}")
        else:
            stderr = delete.stderr.lower()
            # "not found" / "no such branch" → already gone (the
            # previous cleanup pass got it, or the worker never created
            # a local ref). Don't escalate.
            if "not found" in stderr or "no such branch" in stderr:
                log(f"{log_prefix}: local branch {branch!r} already absent")
            else:
                log(
                    f"{log_prefix}: git branch -d {branch} failed "
                    f"({delete.stderr.strip() or delete.returncode}); "
                    f"leaving branch in place"
                )


# ---------------------------------------------------------------------------
# Phase 2 — spawn machinery
# ---------------------------------------------------------------------------


def resolve_claude_bin(
    *,
    preferred: str = CLAUDE_BIN_PREFERRED,
    fallback: str = CLAUDE_BIN_FALLBACK,
) -> str:
    """Return the path to the ``claude`` binary.

    Prefers the spec'd venv path when it exists; otherwise returns the
    PATH-resolved binary name (so ``subprocess.Popen`` will resolve it
    via the shell's normal lookup).
    """
    if pathlib.Path(preferred).is_file():
        return preferred
    return fallback


def _spawn_dir_is_alive(child: pathlib.Path) -> bool:
    """Return True iff ``child`` has a pidfile whose PID is still live.

    Missing/unreadable pidfile → False. ``ProcessLookupError`` or
    ``PermissionError`` from ``os.kill(pid, 0)`` → False (PID recycled
    or no longer ours). Unexpected ``OSError`` → True (be conservative
    rather than reaping a possibly-live spawn).
    """
    pidfile = child / "pidfile"
    if not pidfile.is_file():
        return False
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return True
    return True


def _reap_spawn_dir(
    child: pathlib.Path,
    finished_root: pathlib.Path,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Move a dead spawn dir into ``finished_root/<name>``.

    On name collision, suffix ``.1``, ``.2``, ... so a previous reap
    isn't clobbered. ``OSError`` is swallowed and logged — the next
    pass will retry.
    """
    try:
        finished_root.mkdir(parents=True, exist_ok=True)
        target = finished_root / child.name
        if target.exists():
            i = 1
            while (finished_root / f"{child.name}.{i}").exists():
                i += 1
            target = finished_root / f"{child.name}.{i}"
        child.rename(target)
    except OSError as exc:
        if log is not None:
            log(
                f"[sm-dispatcher] could not reap dead spawn "
                f"{child}: {exc}"
            )


def count_running_spawns(
    spawn_dir: pathlib.Path = SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """Return the number of live spawned subprocesses.

    Walks ``spawn_dir/*/pidfile``. Live spawns count toward the
    returned total; dead spawns are moved to ``spawn_dir/.finished/``
    so a future pass doesn't keep re-checking them.
    """
    if not spawn_dir.is_dir():
        return 0
    finished_root = spawn_dir / ".finished"
    live = 0
    for child in spawn_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == ".finished":
            continue
        if _spawn_dir_is_alive(child):
            live += 1
        else:
            _reap_spawn_dir(child, finished_root, log=log)
    return live


def has_live_spawn_for_issue(
    issue_number: int,
    spawn_dir: pathlib.Path = SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Return True iff a live spawn dir exists for ``issue_number``.

    Scans ``spawn_dir/spawn-<issue_number>-*/`` (active dir only —
    ``.finished/`` is excluded). If any matching dir has a pidfile
    pointing at a live PID, returns True. Any matching dir whose
    pidfile is missing or points at a dead PID is moved into
    ``spawn_dir/.finished/`` so it doesn't clutter future passes.

    Issue #115: previously the dispatcher dedup-ed on the
    ``[SM] spawn-started`` audit comment alone, which made the comment
    a permanent gate — a worker that died after posting the comment
    but before opening a PR could not be replaced without manual
    intervention. The comment is now an audit trail only; ground truth
    is the live spawn dir.
    """
    if not spawn_dir.is_dir():
        return False
    finished_root = spawn_dir / ".finished"
    prefix = f"spawn-{issue_number}-"
    alive = False
    for child in spawn_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == ".finished":
            continue
        if not child.name.startswith(prefix):
            continue
        if _spawn_dir_is_alive(child):
            alive = True
        else:
            _reap_spawn_dir(child, finished_root, log=log)
    return alive


def compose_spawn_prompt(
    issue: dict[str, Any],
    spawn_config: dict[str, str],
) -> str:
    """Render the full prompt text fed to the spawned ``claude`` agent.

    The prompt embeds the issue body verbatim, the artifact label, the
    issue source (author identity), and the role-specific instruction
    trailer with ``{issue_number}`` substituted.
    """
    number = issue.get("number")
    title = issue.get("title") or "(no title)"
    body = issue.get("body") or "(no body)"
    art_label = "art:unknown"
    for name in _label_names(issue):
        if name.startswith("art:") and name in ART_LABEL_WHITELIST:
            art_label = name
            break
    login = _author_login(issue) or "(unknown)"
    source_label = f"source:{login}"

    role = spawn_config["system_prompt_role"]
    trailer = spawn_config["instruction_trailer"].format(issue_number=number)

    if role == "code-worker":
        task_framing = (
            "Your task: implement the change described above. Read the "
            "relevant code first, write a focused diff, run tests, and "
            "open a PR."
        )
    else:
        task_framing = (
            "Your task: produce the research note described above. "
            "Read prior art in the vault, write the note with proper "
            "frontmatter and wikilinks, then post the SM transition "
            "comment when finished."
        )

    # The agent name itself is intentionally left out of the literal
    # prompt — the SM task is repo-anchored, not persona-anchored, and
    # the runtime persona system owns identity rendering. The role
    # label (``code-worker`` / ``research-writer``) carries the
    # behavioral framing.
    return (
        f"You are a {role} agent working on an SM task.\n"
        f"\n"
        f"Issue: #{number}\n"
        f"Title: {title}\n"
        f"Source: {source_label}\n"
        f"Artifact type: {art_label}\n"
        f"\n"
        f"Issue body:\n"
        f"{body}\n"
        f"\n"
        f"{task_framing}\n"
        f"\n"
        f"{trailer}\n"
        f"\n"
        f"Operate as a real engineer would: read the relevant code "
        f"first, test before merging, do not bypass CI hooks. "
        f"Self-merge when CI is green (for code work) or post the "
        f"transition comment (for research work).\n"
    )


def render_spawn_started_comment(
    number: int,
    art_label: str,
    spawn_id: str,
    *,
    runtime: str = "claude-cli",
    timestamp: str | None = None,
) -> str:
    """Produce the literal ``[SM] spawn-started ...`` audit comment."""
    ts = timestamp or _now_iso()
    return (
        f"{SPAWN_STARTED_PREFIX} task=#{number} artifact={art_label} "
        f"runtime={runtime} spawn_id={spawn_id} ts={ts}"
    )


def spawn_agent(
    issue: dict[str, Any],
    art_label: str,
    repo: str,
    *,
    sm_state: str = ACTIVE_SM_LABEL,
    spawn_dir: pathlib.Path = SPAWN_DIR,
    claude_bin: str | None = None,
    post_comment: PostCommentFn = gh_post_comment,
    popen: Callable[..., Any] = subprocess.Popen,
    now_iso: Callable[[], str] = _now_iso,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    clock: Callable[[], float] = None,  # type: ignore[assignment]
) -> str | None:
    """Spawn a detached ``claude`` agent for an SM issue.

    Steps (per issue #101 spec):

      1. Mint ``spawn_id = "spawn-<N>-<unix-ts>"``.
      2. Create ``spawn_dir/<spawn_id>/``.
      3. Compose the prompt + write ``prompt.txt``.
      4. Post ``[SM] spawn-started ...`` audit comment (dedup signal
         for the next dispatcher pass — posted BEFORE the Popen so a
         crash during launch still leaves the dedup marker).
      5. Launch claude detached via ``subprocess.Popen``:
         stdin=open(prompt.txt), stdout/stderr to log files,
         ``start_new_session=True`` so the agent survives the
         dispatcher exiting.
      6. Write PID to ``pidfile``.

    ``sm_state`` selects which SPAWN_MAP row to use; defaults to
    ``sm:selected`` (the v1 worker-spawn path). Issue #107 added the
    ``(sm:reviewing, art:code)`` row, which a later dispatcher change
    will route here with ``sm_state="sm:reviewing"``.

    Returns the ``spawn_id`` on success, or ``None`` if the spawn
    config is missing (unknown ``(sm_state, art:*)`` combination).
    Does NOT wait for the spawned subprocess to complete — the
    dispatcher exits immediately after the Popen returns.
    """
    if clock is None:
        clock = time.time
    spawn_config = SPAWN_MAP.get((sm_state, art_label))
    if spawn_config is None:
        log(
            f"[sm-dispatcher] no spawn config for artifact {art_label!r} "
            f"at state {sm_state!r} on #{issue.get('number')} — skipping spawn"
        )
        return None

    number = issue.get("number")
    if not isinstance(number, int):
        log(
            f"[sm-dispatcher] cannot spawn on non-integer issue "
            f"number: {number!r}"
        )
        return None

    if claude_bin is None:
        claude_bin = resolve_claude_bin()

    spawn_id = f"spawn-{number}-{int(clock())}"
    work_dir = spawn_dir / spawn_id
    work_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = compose_spawn_prompt(issue, spawn_config)
    prompt_path = work_dir / "prompt.txt"
    prompt_path.write_text(prompt_text)

    # Post the [SM] spawn-started audit comment FIRST. If this fails
    # we abort the spawn — without the dedup marker, the next pass
    # would re-spawn the same task. Posting before Popen means a
    # crash-during-launch still leaves the marker, which is the
    # correct dedup semantics (the dispatcher exits after Popen
    # returns; the supervisor cadence will catch the dead pidfile via
    # count_running_spawns on the next pass).
    body = render_spawn_started_comment(
        number, art_label, spawn_id, timestamp=now_iso()
    )
    try:
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post spawn-started on #{number}: "
            f"{exc} — aborting spawn"
        )
        # Re-raise so the caller (main loop) can detect auth / rate
        # limit and bail. Other errors propagate too — the spawn dir
        # is left behind (without a pidfile) and gets reaped on the
        # next pass.
        raise

    stdout_path = work_dir / "stdout.log"
    stderr_path = work_dir / "stderr.log"
    pidfile_path = work_dir / "pidfile"

    # Open prompt as stdin, log files as stdout/stderr. start_new_session
    # detaches the subprocess from the dispatcher's controlling
    # terminal + signal group — the dispatcher process can exit and
    # the agent keeps running.
    stdin_fh = open(prompt_path, "rb")
    stdout_fh = open(stdout_path, "wb")
    stderr_fh = open(stderr_path, "wb")
    try:
        proc = popen(
            [claude_bin, "--print"],
            stdin=stdin_fh,
            stdout=stdout_fh,
            stderr=stderr_fh,
            start_new_session=True,
        )
    finally:
        # Close the parent's view of the FDs — the child inherits its
        # own copies. Keeping them open in the parent would mean the
        # files only fully release when the dispatcher exits.
        stdin_fh.close()
        stdout_fh.close()
        stderr_fh.close()

    pid = getattr(proc, "pid", None)
    if pid is not None:
        pidfile_path.write_text(str(pid))
    log(
        f"[sm-dispatcher] spawned {spawn_id} (pid={pid}) on #{number} "
        f"art={art_label}"
    )
    return spawn_id


# ---------------------------------------------------------------------------
# Trust filter
# ---------------------------------------------------------------------------


@dataclass
class TrustDecision:
    """Outcome of running the trust filter on a single issue."""

    accepted: bool
    reason: str  # human-readable; populated on rejection too for logging
    art_label: str | None = None  # populated on acceptance


def _label_names(issue: dict[str, Any]) -> list[str]:
    raw = issue.get("labels") or []
    names: list[str] = []
    for entry in raw:
        # ``gh issue list --json labels`` returns
        # ``[{"id": ..., "name": ..., "description": ..., "color": ...}, ...]``.
        # Accept bare strings too — keeps the test fixtures readable.
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
        elif isinstance(entry, str):
            names.append(entry)
    return names


def _author_login(issue: dict[str, Any]) -> str | None:
    author = issue.get("author") or {}
    if isinstance(author, dict):
        login = author.get("login")
        if isinstance(login, str):
            return login
    # ``gh`` sometimes returns the bare login string under unusual configs.
    if isinstance(author, str):
        return author
    return None


def _current_sm_label(issue: dict[str, Any]) -> str | None:
    """Return the single whitelisted ``sm:*`` label, or None if not exactly one."""
    names = _label_names(issue)
    sm_labels = [n for n in names if n.startswith("sm:") and n in SM_LABEL_WHITELIST]
    if len(sm_labels) != 1:
        return None
    return sm_labels[0]


def evaluate_trust(
    issue: dict[str, Any],
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    sm_whitelist: frozenset[str] = SM_LABEL_WHITELIST,
    art_whitelist: frozenset[str] = ART_LABEL_WHITELIST,
) -> TrustDecision:
    """Run the v0 trust filter against one ``gh issue list`` payload.

    Returns a :class:`TrustDecision`. On rejection, ``reason`` is a short
    diagnostic string suitable for stderr; on acceptance, ``art_label``
    carries the matched ``art:*`` label so the caller can render it into
    the dispatcher-hello comment without re-scanning.
    """
    login = _author_login(issue)
    if not login or login not in trusted_authors:
        return TrustDecision(
            accepted=False,
            reason=f"untrusted author: {login!r}",
        )

    names = _label_names(issue)
    sm_labels = [n for n in names if n.startswith("sm:")]
    sm_in_whitelist = [n for n in sm_labels if n in sm_whitelist]
    if len(sm_labels) != 1 or len(sm_in_whitelist) != 1:
        return TrustDecision(
            accepted=False,
            reason=(f"expected exactly one whitelisted sm:* label, got {sm_labels!r}"),
        )

    art_labels = [n for n in names if n.startswith("art:") and n in art_whitelist]
    if not art_labels:
        return TrustDecision(
            accepted=False,
            reason=(
                "expected at least one whitelisted art:* label, "
                f"got {[n for n in names if n.startswith('art:')]!r}"
            ),
        )

    # When multiple ``art:*`` labels are set, pick the lexicographically
    # smallest for determinism in the dispatcher-hello payload. v0 isn't
    # required to handle multi-artifact tasks; sorting just keeps the
    # output stable.
    return TrustDecision(
        accepted=True,
        reason="ok",
        art_label=sorted(art_labels)[0],
    )


# ---------------------------------------------------------------------------
# Comment rendering
# ---------------------------------------------------------------------------


def render_hello_comment(
    number: int,
    art_label: str,
    *,
    sm_label: str = ACTIVE_SM_LABEL,
    timestamp: str | None = None,
    version: int = 0,
) -> str:
    """Produce the literal ``[SM] dispatcher-hello ...`` payload."""
    ts = timestamp or _now_iso()
    return (
        f"[SM] dispatcher-hello task=#{number} state={sm_label} "
        f"art={art_label} ts={ts} v={version}"
    )


def render_transition_comment(from_state: str, to_state: str, reason: str) -> str:
    """Produce the literal ``[SM] transition ...`` payload."""
    # Strip the ``sm:`` prefix in the rendered comment to match the
    # spec example: ``from=selected to=reviewing reason="..."``.
    f_short = from_state.removeprefix("sm:")
    t_short = to_state.removeprefix("sm:")
    return f'[SM] transition from={f_short} to={t_short} reason="{reason}"'


# ---------------------------------------------------------------------------
# Issue #128 — verification (smoke-test) machinery
# ---------------------------------------------------------------------------


def render_verify_comment(
    outcome: str,
    number: int,
    *,
    reason: str | None = None,
    route: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Produce a literal ``[SM] verify-{pass,skip,failed} ...`` payload.

    ``outcome`` selects the prefix; the other fields are formatted to
    match the existing ``[SM] xxx key=value ...`` shape used throughout
    the dispatcher's audit trail.
    """
    ts = timestamp or _now_iso()
    if outcome == "pass":
        return f"{VERIFY_PASS_PREFIX} task=#{number} route={route} ts={ts}"
    if outcome == "skip":
        return (
            f"{VERIFY_SKIP_PREFIX} task=#{number} "
            f'reason="{reason or "no recipe matched"}" ts={ts}'
        )
    if outcome == "failed":
        return (
            f"{VERIFY_FAILED_PREFIX} task=#{number} "
            f'reason="{reason or "verification failed"}" ts={ts}'
        )
    raise ValueError(f"unknown verify outcome: {outcome!r}")


def _http_get_body(
    url: str,
    *,
    timeout: float = VERIFY_HTTP_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, str]:
    """Issue a GET, return ``(status, body_text)``.

    Wraps :func:`urllib.request.urlopen` so tests can inject a fake
    opener and avoid actual network I/O. Decodes the body as UTF-8 with
    ``errors='replace'`` — the marker check is a substring match so
    mojibake on the boundary won't matter.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "alice-sm-verify/1"})
    with opener(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()
    if isinstance(raw, bytes):
        body = raw.decode("utf-8", errors="replace")
    else:
        body = str(raw)
    return status, body


def verify_viewer_route(
    *,
    url: str,
    marker: str,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Run the viewer-route smoke test, return a verdict dict.

    Verdict keys:
      - ``outcome``: ``"pass"`` or ``"fail"``
      - ``reason``: short human string (populated on fail)
      - ``route``: URL probed (populated on pass; included on fail for
        the audit comment so Jason can replay it manually)

    Failure modes that count as a *fail* (not a transient bail-out):
      - Connection refused / timeout / DNS error
      - Non-2xx HTTP status
      - 2xx but the marker substring isn't in the response body

    The verifier never raises; transport errors are caught and
    reported as ``outcome="fail"`` so the dispatcher can post the
    ``verify-failed`` audit comment and leave the issue at
    ``sm:reviewing`` for a human to inspect.
    """
    getter = http_get or _http_get_body
    try:
        status, body = getter(url)
    except (urllib.error.URLError, OSError) as exc:
        return {
            "outcome": "fail",
            "reason": f"viewer probe failed: {exc.__class__.__name__}: {exc}",
            "route": url,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "outcome": "fail",
            "reason": f"viewer probe raised {exc.__class__.__name__}: {exc}",
            "route": url,
        }
    if not (200 <= int(status) < 300):
        return {
            "outcome": "fail",
            "reason": f"viewer probe HTTP {status}",
            "route": url,
        }
    if marker not in body:
        return {
            "outcome": "fail",
            "reason": f"marker {marker!r} not found in response body",
            "route": url,
        }
    return {"outcome": "pass", "reason": "viewer marker present", "route": url}


def _touches_viewer(files: Iterable[str]) -> bool:
    return any(p.startswith(VERIFY_VIEWER_PATH_PREFIX) for p in files)


def default_verifier(
    pr_number: int,
    files: list[str],
    *,
    viewer_url: str | None = None,
    viewer_marker: str | None = None,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Default issue-#128 verification recipe dispatcher.

    Picks a verification recipe based on what the merged PR touched.
    v1 only ships the *viewer-route* recipe; anything else returns
    ``outcome="skip"`` with a recipe-not-matched reason so the
    dispatcher can still close the issue (audit-trail visible) without
    pretending we ran a check we didn't.

    Wired into :func:`run` via the ``verify_pr`` keyword argument so
    tests can inject a recipe stub that doesn't open sockets.
    """
    url = viewer_url or os.environ.get(VERIFY_VIEWER_URL_ENV, VERIFY_VIEWER_URL_DEFAULT)
    marker = viewer_marker or os.environ.get(
        VERIFY_VIEWER_MARKER_ENV, VERIFY_VIEWER_MARKER_DEFAULT
    )
    if _touches_viewer(files):
        return verify_viewer_route(url=url, marker=marker, http_get=http_get)
    # Future recipes (dispatcher --check, speaking enqueue-and-assert,
    # research-note path-exists) extend this branch. Until then,
    # anything outside the viewer touch is treated as "no recipe
    # matched" and allowed through with a verify-skip audit comment.
    return {
        "outcome": "skip",
        "reason": "no verification recipe matched (no src/alice_viewer/ files in PR)",
        "route": None,
    }


def _verify_enabled() -> bool:
    raw = os.environ.get(VERIFY_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------


@dataclass
class RunReport:
    """Summary of one dispatcher pass — for tests + stderr logging."""

    polled: int = 0
    posted: int = 0
    skipped_dedup: int = 0
    skipped_trust: int = 0
    posted_numbers: list[int] = field(default_factory=list)
    transitioned: int = 0
    transitions: list[tuple[int, str, str]] = field(
        default_factory=list
    )  # (issue_number, from, to)
    # Phase 1.6 — count of stale-closed-issue sweep transitions. Counted
    # separately from ``transitioned`` so the done-line tells you at a
    # glance whether the missed-window sweep is firing.
    swept: int = 0
    # Phase 2 — count of agent spawns this pass.
    spawned: int = 0
    # Issue numbers + spawn ids for which an agent was spawned. Useful
    # for tests + dry-run reporting.
    spawn_records: list[tuple[int, str, str]] = field(
        default_factory=list
    )  # (issue_number, art_label, spawn_id or "<dry-run>")
    # Issue #127 — count of post-merge working-tree cleanups invoked on
    # the ``sm:reviewing → sm:done`` path. Logged on the done-line so
    # the operator sees whether checkouts are firing.
    cleaned_up: int = 0
    # Issue #128 — verification outcome counters. ``verify_pass`` and
    # ``verify_skip`` both allow the issue through to ``sm:done`` (the
    # latter records that no recipe matched); ``verify_failed`` holds
    # the issue at ``sm:reviewing`` for human inspection.
    verify_pass: int = 0
    verify_skip: int = 0
    verify_failed: int = 0
    # (issue_number, outcome, reason) for the done-line / tests.
    verify_records: list[tuple[int, str, str]] = field(default_factory=list)


def _process_selected(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    has_live_spawn: Callable[[int], bool] | None,
    count_running: Callable[[], int] | None,
    spawn: Callable[[dict[str, Any], str, str], str | None] | None,
    max_concurrent_spawns: int,
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """Hello + T1 (selected → reviewing) + Phase 2 spawn for one sm:selected
    issue.

    Order matters: trust filter → hello (idempotent) → T1 if linked PR
    exists (terminating, since work is already in flight) → otherwise
    Phase 2 spawn (gated by concurrency cap + dedup on a live spawn
    dir for the issue — see :func:`has_live_spawn_for_issue`).
    """
    number = issue["number"]
    decision = evaluate_trust(issue)
    if not decision.accepted:
        log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
        report.skipped_trust += 1
        return

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

    # No linked PR yet — Phase 2 spawn path. Caller passes
    # spawn/count_running/has_live_spawn=None to disable (tests that
    # only care about hello/T1 paths can leave these out).
    if spawn is None or count_running is None or has_live_spawn is None:
        return

    if (ACTIVE_SM_LABEL, art_label) not in SPAWN_MAP:
        log(
            f"[sm-dispatcher] spawn skip #{number}: "
            f"unrecognized artifact {art_label!r}"
        )
        return

    # Dedup on a live spawn dir (issue #115). The historic
    # [SM] spawn-started audit comment is NOT consulted — if the
    # worker died after posting the comment but before opening a PR,
    # we want the next pass to retry, not be permanently gated by the
    # comment. ``has_live_spawn`` also reaps any stale spawn-<N>-* dirs
    # into ``.finished/`` so they don't keep getting re-checked.
    if has_live_spawn(number):
        log(
            f"[sm-dispatcher] spawn skip #{number}: live spawn dir "
            f"already running"
        )
        return

    live = count_running()
    if live >= max_concurrent_spawns:
        log(
            f"[sm-dispatcher] spawn skip #{number}: concurrency cap "
            f"reached ({live}/{max_concurrent_spawns}) — queued for "
            f"next pass"
        )
        return

    if dry_run:
        preview = compose_spawn_prompt(
            issue, SPAWN_MAP[(ACTIVE_SM_LABEL, art_label)]
        )[:240]
        log(
            f"[sm-dispatcher] DRY-RUN would spawn on #{number} "
            f"art={art_label} (running={live}/{max_concurrent_spawns})"
        )
        log(f"[sm-dispatcher] DRY-RUN prompt preview: {preview!r}")
        report.spawned += 1
        report.spawn_records.append((number, art_label, "<dry-run>"))
        return

    try:
        spawn_id = spawn(issue, art_label, repo)
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
    dry_run: bool,
    log: Callable[[str], None],
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
        # PR still open — stay at reviewing.
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
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → building (CI red)")
        return


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


def run(
    *,
    repo: str = DEFAULT_REPO,
    state_path: pathlib.Path,
    list_issues: ListIssuesFn | None = None,
    list_stale_closed: ListIssuesFn | None = None,
    post_comment: PostCommentFn = gh_post_comment,
    edit_labels: EditLabelsFn = gh_edit_labels,
    close_issue: CloseIssueFn = gh_close_issue,
    find_linked_pr: FindLinkedPRFn = gh_find_linked_pr,
    pr_merge_status: PRMergeStatusFn = gh_get_pr_merge_status,
    master_ci_status: MasterCIStatusFn = gh_get_master_ci_status,
    has_live_spawn: Callable[[int], bool] | None = None,
    count_running: Callable[[], int] | None = None,
    spawn: Callable[[dict[str, Any], str, str], str | None] | None = None,
    enable_spawn: bool = True,
    max_concurrent_spawns: int = MAX_CONCURRENT_SPAWNS,
    post_merge_cleanup: PostMergeCleanupFn | None = None,
    enable_cleanup: bool = True,
    worker_repo_path: pathlib.Path = WORKER_REPO_PATH,
    pr_files: PRFilesFn | None = None,
    verify_pr: VerifyFn | None = None,
    enable_verify: bool = True,
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
    if enable_spawn:
        # Default to live production wiring when the caller hasn't
        # provided test fixtures. enable_spawn=False is the test escape
        # hatch — leaves has_live_spawn / count_running / spawn as
        # None, so :func:`_process_selected` short-circuits the spawn
        # branch.
        if has_live_spawn is None:
            def has_live_spawn(number: int) -> bool:
                return has_live_spawn_for_issue(number, SPAWN_DIR, log=log)
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

    report = RunReport()
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
                    has_live_spawn=has_live_spawn,
                    count_running=count_running,
                    spawn=spawn,
                    max_concurrent_spawns=max_concurrent_spawns,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
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
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            else:
                # Phase 1.5 doesn't act on draft / building / validating
                # / done / rejected / blocked. Listed for visibility
                # only.
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
        f"cleaned_up={report.cleaned_up} "
        f"verify_pass={report.verify_pass} "
        f"verify_skip={report.verify_skip} "
        f"verify_failed={report.verify_failed} "
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
