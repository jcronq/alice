"""GitHub → ``inner/tasks/`` reconciler — sm:* labels are the source of truth.

Issue #376. The SM v2 task store has 18 hand-authored entries from
mid-May design sessions; the GitHub issue substrate carries the
actual day-to-day operating state (``sm:draft``, ``sm:selected``,
``sm:building``, ``sm:reviewing``, ``sm:done``, ``sm:rejected``,
``sm:blocked``, ``sm:validating``, ``sm:needs_study``). The two
diverged because nothing wired them together. Issue #375 closed half
the gap by giving Speaking-side workers a task-CLI to call; this
reconciler closes the other half deterministically — no agent in the
loop, no "agent forgot" failure mode.

Cadence
-------

One-shot per invocation, supervised on a 5-minute timer by an s6
service (``sandbox/s6/alice-gh-reconciler/run``). The pattern matches
``alice-sm-dispatcher`` and ``alice-gh-watcher``: flock-based
singleton, exit code drives supervisor backoff.

Reconciliation rules
--------------------

For each watched repo (``jcronq/cozyhem-engine``, ``jcronq/alice``):

1. Fetch issues touched within ``GH_RECONCILER_LOOKBACK_DAYS`` (default
   14) via ``gh api repos/<repo>/issues?state=all&since=...``.
2. For each issue carrying any ``sm:*`` label OR linked to a task by
   the ``github-issue:<repo>#<N>`` tag:

   * Derive the canonical target status (see :func:`derive_target_status`).
     Precedence:

     1. ``pull_request.merged_at`` set → ``done`` with ``merge_ref``.
     2. ``sm:done`` label → ``done``.
     3. ``state == "closed"`` → ``rejected``.
     4. Open: most-advanced ``sm:*`` label.

   * Look up local task by the ``github-issue:<repo>#<N>`` tag. If no
     task exists, create one with status ``draft`` and the derived
     tags. Then transition through the validity graph to the target.
   * If a task exists and is already at the target status: no-op.
   * If a task exists and differs: walk a shortest valid path to the
     target via :func:`shortest_path`. Each hop appends a
     transitions.jsonl row with actor=``alice`` and a structured
     reason.

3. Skip transitions whose required sidecar fields are unavailable
   (``validating → done`` needs ``validation_evidence``; ``→ blocked``
   needs ``unblocked_by`` — only the direct ``sm:blocked`` → ``blocked``
   mapping satisfies the second, with a synthetic ``unblocked_by``).

Idempotency
-----------

Rerunning with no GitHub change produces zero transitions:

* ``find_task_for_issue`` returns the most-recently-updated record
  (open or terminal) for the issue tag — so a re-run on a done
  task finds the same task and computes a zero-length path.
* Path-finding short-circuits when ``current == target``.

Concurrency
-----------

The reconciler shares the task store with the Speaking-side auto-fix
dispatcher. ``task_store.TaskStore`` flock's ``index.jsonl`` for every
write; the reconciler relies on that lock and adds no second layer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from alice_forge.task_store import (
    InvalidTransition,
    TERMINAL_STATES,
    TaskRecord,
    TaskStore,
    VALID_TRANSITIONS,
    default_root,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default repos to reconcile. Override with ``GH_RECONCILER_REPOS``
#: (comma-separated ``owner/name`` list). Matches the substrate the
#: SM v2 dispatcher already polls.
DEFAULT_REPOS: tuple[str, ...] = ("jcronq/cozyhem-engine", "jcronq/alice")

#: Default lookback window in days. 14 catches label changes on
#: month-old issues without scanning all-time. Override with
#: ``GH_RECONCILER_LOOKBACK_DAYS``.
DEFAULT_LOOKBACK_DAYS = 14

#: Page size for the ``gh api`` call. 100 is GitHub's max; we paginate
#: defensively in case a repo has more than that within the lookback
#: window.
PAGE_SIZE = 100

#: Hard cap on pagination so a misconfigured filter can't burn the
#: rate limit indefinitely.
MAX_PAGES = 10


# ---------------------------------------------------------------------------
# Label → status mapping
# ---------------------------------------------------------------------------

#: Canonical label → status mapping (issue #376 §Label → status mapping).
#: ``sm:needs_study`` maps to ``reviewing`` — task-0002 uses the legacy
#: ``review`` state but the task-store CLI never *writes* ``review``,
#: only tolerates it on read. The closest write-able state for
#: "needs review of the proposal" is ``reviewing``.
LABEL_TO_STATUS: dict[str, str] = {
    "sm:draft": "draft",
    "sm:needs_study": "reviewing",
    "sm:reviewing": "reviewing",
    "sm:selected": "selected",
    "sm:building": "building",
    "sm:validating": "validating",
    "sm:done": "done",
    "sm:rejected": "rejected",
    "sm:blocked": "blocked",
}

#: Precedence used when an issue carries multiple ``sm:*`` labels.
#: Higher index = "more advanced" along the SM v2 pipeline. The two
#: terminal states top the list because labelers occasionally leave
#: legacy intermediate labels in place after marking sm:done.
_STATUS_PRECEDENCE: list[str] = [
    "draft",       # 0
    "selected",    # 1
    "blocked",     # 2 — parallel branch; sits below the build chain
    "building",    # 3
    "reviewing",   # 4
    "validating",  # 5
    "rejected",    # 6 — terminal
    "done",        # 7 — terminal
]


def _label_names(issue: dict[str, Any]) -> set[str]:
    """Extract the set of label name strings from an issue payload."""
    out: set[str] = set()
    for entry in issue.get("labels") or []:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                out.add(name)
        elif isinstance(entry, str):
            out.add(entry)
    return out


def _art_label(labels: set[str]) -> Optional[str]:
    """Return the first ``art:*`` label found, or None."""
    for name in sorted(labels):
        if name.startswith("art:"):
            return name
    return None


def _most_advanced_sm_label(labels: set[str]) -> Optional[str]:
    """Pick the ``sm:*`` label that maps to the most-advanced status.

    Unknown ``sm:*`` labels are skipped with a warning; the caller
    treats "no known mapping" as "leave the task unchanged" rather
    than promoting a typo'd label into a transition.
    """
    best_status: Optional[str] = None
    best_idx = -1
    for name in labels:
        if not name.startswith("sm:"):
            continue
        mapped = LABEL_TO_STATUS.get(name)
        if mapped is None:
            log.warning("unknown sm:* label %r — leaving task unchanged", name)
            continue
        try:
            idx = _STATUS_PRECEDENCE.index(mapped)
        except ValueError:  # pragma: no cover — defensive
            continue
        if idx > best_idx:
            best_idx = idx
            best_status = mapped
    return best_status


# ---------------------------------------------------------------------------
# Target derivation
# ---------------------------------------------------------------------------


def derive_target_status(
    issue: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Compute ``(target_status, merge_ref)`` for an issue payload.

    Precedence:

    1. Merged PR → ``done`` with ``html_url`` as ``merge_ref``.
    2. ``sm:done`` label → ``done``.
    3. ``state == "closed"`` (any reason) → ``rejected``.
    4. Most-advanced ``sm:*`` label among the ones we recognise.
    5. No mapping → ``(None, None)``; caller skips.

    The PR-merge case bypasses the ``sm:*`` precedence on purpose:
    a label that hasn't caught up to the merge is stale, and we trust
    the GitHub state over an out-of-date sticker.
    """
    labels = _label_names(issue)
    pr = issue.get("pull_request") or {}
    if isinstance(pr, dict) and pr.get("merged_at"):
        return "done", issue.get("html_url")
    if "sm:done" in labels:
        # Surface a merge_ref if the issue happens to be a (closed)
        # PR — otherwise the open-issue-flagged-done path has no URL
        # to attach. ``html_url`` is the canonical pointer either way.
        return "done", issue.get("html_url") if pr else None
    if issue.get("state") == "closed":
        return "rejected", None
    label_status = _most_advanced_sm_label(labels)
    return label_status, None


# ---------------------------------------------------------------------------
# Path-finding through the SM v2 transition graph
# ---------------------------------------------------------------------------


def shortest_path(
    current: str,
    target: str,
    *,
    have_merge_ref: bool,
    have_validation_evidence: bool,
    have_unblocked_by: bool,
) -> Optional[list[str]]:
    """Return the shortest sequence of states ``[s1, s2, ..., target]``
    that walks from ``current`` to ``target`` using only edges whose
    sidecar requirements are satisfied by the caller's available
    arguments.

    Returns ``None`` when no satisfiable path exists. Returns ``[]``
    when ``current == target`` (no-op).
    """
    if current == target:
        return []
    if current in TERMINAL_STATES:
        return None

    # BFS over the validity graph, dropping edges whose required
    # sidecar args aren't available.
    queue: deque[tuple[str, list[str]]] = deque([(current, [])])
    visited: set[str] = {current}
    while queue:
        node, path = queue.popleft()
        allowed = set(VALID_TRANSITIONS.get(node, frozenset()))
        # building → done is the self-merge shortcut; only available
        # when we have a merge_ref.
        if node == "building" and have_merge_ref:
            allowed = set(allowed) | {"done"}
        for nxt in sorted(allowed):
            if nxt in visited:
                continue
            # Drop edges that need sidecar args we don't have.
            if nxt == "blocked" and not have_unblocked_by:
                continue
            if (
                node == "validating"
                and nxt == "done"
                and not have_validation_evidence
            ):
                continue
            new_path = path + [nxt]
            if nxt == target:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))
    return None


# ---------------------------------------------------------------------------
# Task lookup (any-status, not just open)
# ---------------------------------------------------------------------------


ISSUE_TAG_PREFIX = "github-issue:"


def issue_tag(repo: str, number: int) -> str:
    """Canonical tag identifying a (repo, issue#) pair."""
    return f"{ISSUE_TAG_PREFIX}{repo}#{number}"


def find_task_for_issue(store: TaskStore, tag: str) -> Optional[dict[str, Any]]:
    """Return the most-recently-updated index entry carrying ``tag``,
    regardless of status.

    Unlike :meth:`TaskStore.find_by_tag`, this includes terminal
    tasks. That's required for idempotency: once an issue's task has
    transitioned to ``done``, the reconciler must keep returning the
    same task on subsequent runs so it computes a zero-length path
    (no new transitions emitted). The store's ``find_by_tag``
    filters terminals on purpose for the dispatcher's lookup
    semantics; the reconciler needs the opposite behavior.
    """
    candidates = [
        entry
        for entry in store.iter_index()
        if tag in (entry.get("tags") or [])
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: e.get("updated", ""), reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# gh CLI shim
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Invoke ``gh`` with ``args``, return stdout. Raises on non-zero.

    Tests patch this via the ``gh_runner=`` kwarg on :func:`reconcile`.
    """
    result = subprocess.run(
        ["gh"] + args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def fetch_recent_issues(
    repo: str,
    *,
    lookback_days: int,
    gh_runner: Callable[[list[str]], str] = _run_gh,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Page through ``gh api repos/<repo>/issues`` and return one combined
    list of issue payloads with ``updated_at`` newer than the cutoff.

    The endpoint returns both regular issues and PRs (PRs carry a
    ``pull_request`` sub-object). We keep both — PRs are how
    ``merge_ref`` gets recorded.
    """
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        endpoint = (
            f"repos/{repo}/issues"
            f"?state=all"
            f"&since={since}"
            f"&per_page={PAGE_SIZE}"
            f"&page={page}"
        )
        raw = gh_runner(["api", endpoint])
        try:
            batch = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            log.error("malformed JSON from gh api for %s page %d: %s", repo, page, exc)
            break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return out


# ---------------------------------------------------------------------------
# Reconciliation core
# ---------------------------------------------------------------------------


def _initial_tags(repo: str, number: int, art_label: Optional[str]) -> list[str]:
    """Tag set for a freshly-created github-synced task."""
    tags = [repo, "github-synced", issue_tag(repo, number)]
    if art_label:
        tags.append(art_label)
    return sorted(set(tags))


def _transition_reason(prev: str, target: str, repo: str, number: int) -> str:
    """Human-readable reason for a reconciler-driven transition."""
    return (
        f"label changed from {prev!r} to {target!r} "
        f"(reconciled from {repo}#{number})"
    )


def reconcile_issue(
    store: TaskStore,
    repo: str,
    issue: dict[str, Any],
    *,
    now: Optional[str] = None,
) -> dict[str, int]:
    """Reconcile a single issue against the task store.

    Returns a small counters dict so :func:`reconcile` can roll up
    per-repo and per-run totals. Keys:

    * ``created``         — new task allocated.
    * ``transitions``     — transitions.jsonl rows appended.
    * ``skipped``         — issue had no sm:* mapping and no existing task.
    * ``invalid_path``    — derived target had no satisfiable path; warning logged.
    """
    counters = {"created": 0, "transitions": 0, "skipped": 0, "invalid_path": 0}
    number = issue.get("number")
    if not isinstance(number, int):
        log.warning("issue payload missing 'number': %r", issue.get("title"))
        counters["skipped"] += 1
        return counters

    target_status, merge_ref = derive_target_status(issue)
    if target_status is None:
        # No mapping; only act if a task already exists (would have
        # been seeded by a prior label that's since been removed).
        existing = find_task_for_issue(store, issue_tag(repo, number))
        if existing is None:
            counters["skipped"] += 1
            return counters
        # Existing task with no current SM label — leave alone but
        # log so the operator notices an issue that's lost its sm:* sticker.
        log.info(
            "%s#%d has tracking task %s but no sm:* label — leaving alone",
            repo,
            number,
            existing.get("id"),
        )
        counters["skipped"] += 1
        return counters

    labels = _label_names(issue)
    art = _art_label(labels)
    tag = issue_tag(repo, number)
    existing = find_task_for_issue(store, tag)

    if existing is None:
        # Allocate a new task in draft, then walk to the target.
        title = str(issue.get("title") or f"{repo}#{number}")
        record = store.create(
            title=title,
            actor="alice",
            artifact_type="code",
            source=f"{repo}#{number}",
            tags=_initial_tags(repo, number, art),
            reason=f"Reconciled from {repo}#{number} ({target_status})",
            now=now,
        )
        counters["created"] += 1
        log.info("created %s for %s#%d (target=%s)", record.id, repo, number, target_status)
        current_status = "draft"
        task_id = record.id
    else:
        current_status = str(existing.get("status") or "draft")
        task_id = str(existing["id"])

    if current_status == target_status:
        return counters

    # Terminal source: if the local task is already terminal but the
    # issue diverged (e.g. re-opened), we can't transition out per
    # SM v2 rules. Log and move on — a future enhancement could
    # allocate a new task for the new lifecycle.
    if current_status in TERMINAL_STATES:
        log.warning(
            "%s already terminal (%s) but %s#%d derives %s — leaving alone",
            task_id,
            current_status,
            repo,
            number,
            target_status,
        )
        counters["skipped"] += 1
        return counters

    have_merge_ref = merge_ref is not None
    path = shortest_path(
        current_status,
        target_status,
        have_merge_ref=have_merge_ref,
        have_validation_evidence=False,
        # Direct sm:blocked → blocked supplies its own unblocked_by;
        # we set the flag to True so path-finder will consider blocked
        # as a destination, then guard the actual update call below.
        have_unblocked_by=(target_status == "blocked"),
    )
    if path is None:
        log.warning(
            "no satisfiable path from %s to %s for %s#%d (task %s) — skipping",
            current_status,
            target_status,
            repo,
            number,
            task_id,
        )
        counters["invalid_path"] += 1
        return counters

    prev = current_status
    for step in path:
        kwargs: dict[str, Any] = {
            "actor": "alice",
            "reason": _transition_reason(prev, step, repo, number),
            "now": now,
        }
        if step == "done" and merge_ref and prev == "building":
            kwargs["merge_ref"] = merge_ref
        elif step == "done" and merge_ref:
            # Non-shortcut done — still record merge_ref so the task
            # carries the PR URL for downstream consumers.
            kwargs["merge_ref"] = merge_ref
        if step == "blocked":
            kwargs["unblocked_by"] = (
                f"GitHub issue {repo}#{number} carries sm:blocked label"
            )
        try:
            store.update(task_id, status=step, **kwargs)
        except InvalidTransition as exc:
            log.error(
                "invalid transition %s → %s on %s (%s#%d): %s",
                prev,
                step,
                task_id,
                repo,
                number,
                exc,
            )
            counters["invalid_path"] += 1
            break
        counters["transitions"] += 1
        log.info(
            "transitioned %s: %s → %s (%s#%d)", task_id, prev, step, repo, number
        )
        prev = step

    return counters


def reconcile(
    repos: list[str],
    *,
    store: TaskStore,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    gh_runner: Callable[[list[str]], str] = _run_gh,
    now: Optional[datetime] = None,
) -> dict[str, dict[str, int]]:
    """Run one reconciliation pass over the given repos.

    Returns a per-repo dict of counters plus a ``__total__`` key. The
    caller (the CLI ``main()`` and the s6-supervised invocation) logs
    a structured heartbeat from this return value.
    """
    rolled: dict[str, dict[str, int]] = {
        "__total__": {
            "created": 0,
            "transitions": 0,
            "skipped": 0,
            "invalid_path": 0,
            "issues_seen": 0,
        }
    }
    now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    for repo in repos:
        log.info("reconciling %s (lookback=%dd)", repo, lookback_days)
        try:
            issues = fetch_recent_issues(
                repo,
                lookback_days=lookback_days,
                gh_runner=gh_runner,
                now=now,
            )
        except Exception as exc:  # gh CLI failures are noisy but recoverable
            log.error("failed to fetch issues for %s: %s", repo, exc)
            rolled[repo] = {
                "created": 0,
                "transitions": 0,
                "skipped": 0,
                "invalid_path": 0,
                "issues_seen": 0,
                "error": 1,
            }
            continue
        repo_counters = {
            "created": 0,
            "transitions": 0,
            "skipped": 0,
            "invalid_path": 0,
            "issues_seen": len(issues),
        }
        for issue in issues:
            sub = reconcile_issue(store, repo, issue, now=now_iso)
            for key in ("created", "transitions", "skipped", "invalid_path"):
                repo_counters[key] += sub.get(key, 0)
        rolled[repo] = repo_counters
        for key in ("created", "transitions", "skipped", "invalid_path", "issues_seen"):
            rolled["__total__"][key] += repo_counters[key]
    return rolled


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_repos_env(value: Optional[str]) -> list[str]:
    if not value:
        return list(DEFAULT_REPOS)
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or list(DEFAULT_REPOS)


def _parse_lookback_env(value: Optional[str]) -> int:
    if not value:
        return DEFAULT_LOOKBACK_DAYS
    try:
        days = int(value)
    except ValueError:
        log.warning(
            "GH_RECONCILER_LOOKBACK_DAYS=%r is not an int; using default %d",
            value,
            DEFAULT_LOOKBACK_DAYS,
        )
        return DEFAULT_LOOKBACK_DAYS
    if days < 1:
        log.warning(
            "GH_RECONCILER_LOOKBACK_DAYS=%d < 1; using default %d",
            days,
            DEFAULT_LOOKBACK_DAYS,
        )
        return DEFAULT_LOOKBACK_DAYS
    return days


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice-gh-reconciler",
        description="Reconcile GitHub sm:* label state into inner/tasks/",
    )
    parser.add_argument(
        "--repos",
        help="Comma-separated <user>/<repo> list. Default: GH_RECONCILER_REPOS env "
        "or the built-in default (jcronq/cozyhem-engine,jcronq/alice).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        help="Days to look back for issue activity. Default: GH_RECONCILER_LOOKBACK_DAYS "
        f"env or {DEFAULT_LOOKBACK_DAYS}.",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        help="Override the tasks directory (default: $TASKS_DIR or ~/alice-mind/inner/tasks).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("GH_RECONCILER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_argparser().parse_args(argv)
    repos = (
        [r.strip() for r in args.repos.split(",") if r.strip()]
        if args.repos
        else _parse_repos_env(os.environ.get("GH_RECONCILER_REPOS"))
    )
    lookback = (
        args.lookback_days
        if args.lookback_days is not None
        else _parse_lookback_env(os.environ.get("GH_RECONCILER_LOOKBACK_DAYS"))
    )
    root = args.root or default_root()
    store = TaskStore(root)
    rolled = reconcile(repos, store=store, lookback_days=lookback)
    log.info("gh-reconciler heartbeat: %s", json.dumps(rolled, sort_keys=True))
    # Exit non-zero if any repo errored, so the s6 supervisor logs it.
    if any("error" in v for v in rolled.values() if isinstance(v, dict)):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
