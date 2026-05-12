"""State Machine v0/v1.5 dispatcher — ``gh``-driven label-driven dispatcher.

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
  3. For ``sm:reviewing`` issues (Phase 1.5 T2/T3):
     - If the linked PR is merged AND master CI on the merge commit
       is green → relabel ``sm:done``, close the issue.
     - If the linked PR is merged AND master CI is red → relabel
       ``sm:building`` (do NOT close, do NOT spawn anything yet).
     - If still pending or PR still open, stay.

Phase 1.5 explicitly does NOT spawn agents, handle ``sm:draft``, or
route amendments. Those land in v1+.

The script is intended to be invoked on a cadence by s6 (later phase);
right now it runs by hand via ``python -m alice_sm.dispatcher``. The
``--dry-run`` flag prints the comments / transitions that would be
posted without touching GitHub or the state file — useful for tests
and manual verification.
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
    """

    version: int = STATE_VERSION
    hello_commented: list[int] = field(default_factory=list)

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hello_commented": list(self.hello_commented),
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
    return DispatcherState(version=STATE_VERSION, hello_commented=numbers)


def save_state(state_path: pathlib.Path, state: DispatcherState) -> None:
    """Atomically replace the state file. Caps the seen-issue list."""
    if len(state.hello_commented) > SEEN_ISSUE_CAP:
        overflow = len(state.hello_commented) - SEEN_ISSUE_CAP
        del state.hello_commented[:overflow]
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
        "number,title,labels,author,createdAt",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    return payload


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
        "number,title,labels,author,createdAt",
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
    return [
        issue
        for issue in payload
        if any(n in SM_LABEL_WHITELIST for n in _label_names(issue))
    ]


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
        "number,title,labels,author,createdAt",
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
    """Return ``{merged, merge_commit_oid, pr_url}`` for a PR."""
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "state,mergeCommit,url",
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return {"merged": False, "merge_commit_oid": None, "pr_url": None}
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return {"merged": False, "merge_commit_oid": None, "pr_url": None}
    merge_commit = payload.get("mergeCommit") or {}
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    return {
        "merged": payload.get("state") == "MERGED",
        "merge_commit_oid": oid,
        "pr_url": payload.get("url"),
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


# Callable aliases — tests inject fakes here without monkeypatching the
# module-level names.
ListIssuesFn = Callable[[str], list[dict[str, Any]]]
PostCommentFn = Callable[[str, int, str], None]
EditLabelsFn = Callable[..., None]
CloseIssueFn = Callable[[str, int], None]
FindLinkedPRFn = Callable[[str, int], dict[str, Any] | None]
PRMergeStatusFn = Callable[[str, int], dict[str, Any]]
MasterCIStatusFn = Callable[[str, str], dict[str, Any]]


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


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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


def _process_selected(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """Hello + T1 (selected → reviewing) for a single sm:selected issue."""
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
    if pr is None:
        return
    # T1 fires only when the linked PR is still OPEN. ``gh_find_linked_pr``
    # queries ``--state all`` (so the T2/T3 path can find merged PRs); we
    # filter here so an sm:selected issue whose PR has already merged or
    # closed doesn't get bounced to sm:reviewing — that lifecycle stage
    # is past.
    pr_state = (pr.get("state") or "").upper()
    if pr_state != "OPEN":
        log(
            f"[sm-dispatcher] #{number} selected but linked PR is {pr_state!r} "
            f"(not OPEN) — not transitioning to reviewing"
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
        report.transitions.append((number, ACTIVE_SM_LABEL, REVIEWING_SM_LABEL))
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


def _process_reviewing(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    close_issue: CloseIssueFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """T2 (reviewing → done) and T3 (reviewing → building) for one issue."""
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
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == REVIEWING_SM_LABEL:
                _process_reviewing(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    close_issue=close_issue,
                    find_linked_pr=find_linked_pr,
                    pr_merge_status=pr_merge_status,
                    master_ci_status=master_ci_status,
                    dry_run=dry_run,
                    log=log,
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
        f"skipped_dedup={report.skipped_dedup} "
        f"skipped_trust={report.skipped_trust}"
    )
    return 0, report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One pass of the State Machine v0/v1.5 dispatcher."
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
