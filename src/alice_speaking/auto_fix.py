"""Speaking-side wiring for auto-fix dispatcher bookkeeping.

PR #262 added :func:`alice_forge.gh_state_mirror.write_dispatched_inflight`
so Thinking's dispatcher can suppress duplicate ``attempt-issue-fix``
surfaces while a worker is mid-flight but hasn't yet pushed a branch /
opened a PR. The race-window write happens here: when Speaking's
``_dispatch_subagent`` is about to spawn a worker, we sniff the prompt
for the auto-fix template header and — if it matches — write the
in-flight note BEFORE the asyncio task starts, so the gh-state mirror
is up-to-date from the moment work begins.

Issue #375 extends the same hook with SM v2 task tracking: every
auto-fix dispatch also creates an ``inner/tasks/task-NNNN`` entry that
follows the worker through ``draft → selected → building`` at intake
and ``building → done`` (or ``→ blocked``) at completion. The
``bg-<handle>`` is tagged onto the task so the completion handler can
look it up without keeping in-process state.

Procedural-logic-in-code per Jason's feedback ("procedural logic lives
in code, not agent instructions"): the LLM doesn't need to call a
separate MCP tool or remember to write the in-flight record. The
template (cortex-memory/reference/auto-fix-worker-prompt.md) defines a
stable leading line we can parse, and this module turns that detection
into the bookkeeping side-effect.

Design: cortex-memory/research/2026-05-19-dispatched-inflight-speaking-wiring.md
Upstream: PR #262 (write_dispatched_inflight implementation).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from alice_forge import gh_state_mirror
from alice_forge.task_store import (
    InvalidTransition,
    TaskStore,
    TaskStoreError,
    default_root,
)


log = logging.getLogger(__name__)


# Matches the leading line of the auto-fix worker prompt template at
# cortex-memory/reference/auto-fix-worker-prompt.md. The shape is
# stable — if the template changes, this regex and the template must
# move together (the test pins the exact format).
_AUTO_FIX_HEADER_RE = re.compile(
    r"^You are an auto-fix worker for issue #(?P<number>\d+) "
    r"in (?P<repo>[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+) "
    r"from @(?P<author>[A-Za-z0-9_\-]+)\.",
    re.MULTILINE,
)


def parse_auto_fix_dispatch(prompt: str) -> Optional[tuple[str, int]]:
    """Return ``(repo, issue_number)`` if ``prompt`` is an auto-fix
    worker dispatch, else ``None``.

    The match keys off the verbatim template leading line — any
    deviation (paraphrased prompt, missing repo slug, etc.) falls
    through to ``None`` so unrelated subagent dispatches don't get a
    spurious in-flight write.
    """
    match = _AUTO_FIX_HEADER_RE.search(prompt or "")
    if match is None:
        return None
    try:
        number = int(match.group("number"))
    except (TypeError, ValueError):
        return None
    return match.group("repo"), number


def record_auto_fix_inflight(
    prompt: str,
    worker_id: str,
) -> Optional[Path]:
    """Write the dispatched-in-flight gh-state note for an auto-fix
    worker spawn. Returns the note path on success, ``None`` when the
    prompt isn't an auto-fix dispatch or the write fails.

    Called from :meth:`SpeakingDaemon._dispatch_subagent` BEFORE the
    asyncio task is created, so the suppression record exists from the
    moment the worker starts. Failure to write is non-fatal — the
    worst case is one duplicate dispatcher surface, far better than
    crashing the dispatch on a transient FS error.
    """
    parsed = parse_auto_fix_dispatch(prompt)
    if parsed is None:
        return None
    repo, number = parsed
    try:
        path = gh_state_mirror.write_dispatched_inflight(
            repo, number, worker_id, title=""
        )
    except Exception:  # noqa: BLE001
        # Don't let a bookkeeping write block the worker. The 4-hour
        # timeout cleanup in gh_state_mirror.main() will reap any
        # stale record; if the worker succeeds and opens a PR, the
        # normal cron-overwrite path replaces this record anyway.
        log.exception(
            "auto-fix in-flight write failed for %s#%d (worker %s)",
            repo,
            number,
            worker_id,
        )
        return None
    log.info(
        "auto-fix in-flight recorded for %s#%d (worker %s) -> %s",
        repo,
        number,
        worker_id,
        path,
    )
    return path


# ---------------------------------------------------------------------------
# SM v2 task tracking (issue #375)


# Matches the first PR URL emitted by a worker subagent in its final
# text. The auto-fix worker template ends with the draft PR URL so we
# key off it directly. Lenient — handles HTTPS GitHub URLs only,
# which is what ``gh pr create`` returns.
_PR_URL_RE = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/pull/\d+"
)


def _worker_tag(worker_id: str) -> str:
    """The tag we attach to a task so the completion handler can find it."""
    return f"worker:{worker_id}"


def _issue_tag(repo: str, number: int) -> str:
    """``repo#N`` tag — also lets ``task list --tag <repo>#<N>`` find the entry."""
    return f"{repo}#{number}"


def record_auto_fix_task_intake(
    prompt: str,
    worker_id: str,
    *,
    title: Optional[str] = None,
    tasks_root: Optional[Path] = None,
) -> Optional[str]:
    """Create an SM v2 task entry for an auto-fix worker dispatch.

    Called from :meth:`SpeakingDaemon._dispatch_subagent` next to
    :func:`record_auto_fix_inflight`. Returns the new ``task-NNNN`` id
    on success, ``None`` when the prompt isn't an auto-fix dispatch or
    the store write fails.

    The task starts in ``draft``, immediately transitions to ``selected``
    (auto-selected per Jason's hard constraint — no human gate on the
    happy path), then to ``building`` (worker is being spawned right
    now). Tags include the repo+issue ref and the worker handle so the
    completion handler can look up the task without in-process state.

    Failure is non-fatal — same policy as ``record_auto_fix_inflight``.
    The worst case is a missing task entry; the worker still runs.
    """
    parsed = parse_auto_fix_dispatch(prompt)
    if parsed is None:
        return None
    repo, number = parsed
    root = tasks_root or default_root()
    store = TaskStore(root)
    task_title = title or f"Auto-fix {repo}#{number}"
    tags = sorted({repo, "auto-fix", _issue_tag(repo, number), _worker_tag(worker_id)})
    try:
        record = store.create(
            title=task_title,
            actor="speaking",
            artifact_type="code",
            source=_issue_tag(repo, number),
            tags=tags,
            reason=f"Auto-fix dispatch for {repo}#{number} (worker {worker_id})",
        )
        store.update(
            record.id,
            status="selected",
            actor="speaking",
            reason="Auto-select on intake (Jason's hard constraint: no human gate)",
        )
        store.update(
            record.id,
            status="building",
            actor="speaking",
            reason=f"Dispatcher spawning worker {worker_id}",
        )
    except (TaskStoreError, OSError):
        log.exception(
            "auto-fix task intake failed for %s#%d (worker %s)",
            repo,
            number,
            worker_id,
        )
        return None
    log.info(
        "auto-fix task intake recorded for %s#%d (worker %s) -> %s",
        repo,
        number,
        worker_id,
        record.id,
    )
    return record.id


def record_auto_fix_task_complete(
    worker_id: str,
    result_text: str,
    *,
    is_error: bool,
    tasks_root: Optional[Path] = None,
) -> Optional[str]:
    """Transition the auto-fix task for ``worker_id`` to its terminal state.

    Called from :func:`alice_speaking._dispatch.handle_background_task_complete`
    when a subagent finishes. Looks up the task by the
    ``worker:<id>`` tag. On success (worker returned text containing a
    GitHub PR URL): transitions ``building → done`` with the PR URL as
    ``merge_ref`` (per Jason's self-merge norm — opening the draft PR
    is effectively done from the dispatcher's perspective). On error
    or no PR URL: transitions ``building → blocked`` with the failure
    reason so thinking surfaces it.

    Returns the task id on success, ``None`` if no matching task is
    found (non-auto-fix dispatch, or task already terminal).
    """
    root = tasks_root or default_root()
    store = TaskStore(root)
    entry = store.find_by_tag(_worker_tag(worker_id))
    if entry is None:
        # Not an auto-fix dispatch we tracked, or the task is already
        # terminal — either way, nothing to update.
        return None
    task_id = entry["id"]

    pr_match = _PR_URL_RE.search(result_text or "")
    try:
        if is_error or not pr_match:
            reason = (
                "Worker reported error" if is_error else "Worker returned no PR URL"
            )
            store.update(
                task_id,
                status="blocked",
                actor="speaking",
                reason=reason,
                unblocked_by="speaking or jason to inspect worker output",
            )
        else:
            pr_url = pr_match.group(0)
            store.update(
                task_id,
                status="done",
                actor="speaking",
                reason=f"Worker {worker_id} opened draft PR; self-merge norm",
                merge_ref=pr_url,
            )
    except InvalidTransition:
        # Task was already advanced past building (race against a
        # manual update). Don't crash — just log and move on.
        log.exception(
            "auto-fix task %s could not be transitioned to terminal", task_id
        )
        return None
    except (TaskStoreError, OSError):
        log.exception("auto-fix task %s update failed at completion", task_id)
        return None
    log.info(
        "auto-fix task %s transitioned at completion (worker %s, error=%s)",
        task_id,
        worker_id,
        is_error,
    )
    return task_id


__all__ = [
    "parse_auto_fix_dispatch",
    "record_auto_fix_inflight",
    "record_auto_fix_task_intake",
    "record_auto_fix_task_complete",
]
