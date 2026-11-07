"""Worker marker files — the Speaking-side writers for issue #789.

Every background sub-agent (``Agent(…)`` → ``bg-<handle>``) Alice
spawns now leaves two traces in ``inner/notes/``:

* ``worker-<bg_id>.json`` at dispatch time — written by
  :func:`write_dispatch_marker` from
  :meth:`alice_speaking.daemon.Daemon._dispatch_subagent`.
* ``worker-<bg_id>-done.json`` at completion — written by
  :func:`write_done_marker` from
  :func:`alice_speaking._dispatch.handle_background_task_complete`.

These files survive container restarts (the volume is bind-mounted),
unlike ``/tmp/claude-*/.../tasks/<bg>.output`` which is session-local.
The memory worker's ``derive_tasks_workers_events`` step merges the
two markers into one ``workers`` row in ``alice-state.db``.

Both writes are **best-effort**: an OS error dropping the marker is
logged and swallowed. The write path cannot be allowed to break the
dispatch/completion flow — a missing marker is a lost observability
row, not a lost worker.

See also:

* ``feedback_dont_edit_inbox_notes_in_flight`` — hence two files
  instead of edit-in-place.
* ``src/alice_state_db/derive.py::_derive_workers`` — the reader.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: pathlib.Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` to ``path`` via tempfile + ``os.rename``.

    Mirrors :meth:`alice_forge.task_store.TaskStore._atomic_write_text`:
    tempfile in the same directory so the rename is atomic on POSIX,
    swallows ``FileNotFoundError`` on cleanup.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=False, default=str)
            fh.write("\n")
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _notes_dir(mind_dir: Optional[pathlib.Path]) -> Optional[pathlib.Path]:
    """Resolve ``<mind>/inner/notes/`` or ``None`` when mind is unset.

    Returning ``None`` is how callers signal "no mind configured" —
    typically in tests that spin up a Daemon without a real vault.
    The marker writers short-circuit silently in that case; the
    tests exercising the marker contract point ``mind_dir`` at a
    tmp path.
    """
    if mind_dir is None:
        return None
    return pathlib.Path(mind_dir) / "inner" / "notes"


def write_dispatch_marker(
    mind_dir: Optional[pathlib.Path],
    *,
    bg_id: str,
    description: str,
    kind: Optional[str] = None,
    label: Optional[str] = None,
    repo: Optional[str] = None,
    gh_issue: Optional[int] = None,
    task_id: Optional[str] = None,
    surface_path: Optional[str] = None,
    stdout_path: Optional[str] = None,
    worktree_path: Optional[str] = None,
    started_at: Optional[str] = None,
) -> Optional[pathlib.Path]:
    """Drop ``inner/notes/worker-<bg_id>.json``.

    Returns the path written, or ``None`` if ``mind_dir`` wasn't
    configured. Swallows OS errors.
    """
    notes = _notes_dir(mind_dir)
    if notes is None:
        return None
    path = notes / f"worker-{bg_id}.json"
    payload: dict[str, Any] = {
        "tag": "worker-dispatched",
        "bg_id": bg_id,
        "description": description,
        "started_at": started_at or _utc_now_iso(),
    }
    if kind is not None:
        payload["kind"] = kind
    if label is not None:
        payload["label"] = label
    if repo is not None:
        payload["repo"] = repo
    if gh_issue is not None:
        payload["gh_issue"] = gh_issue
    if task_id is not None:
        payload["task_id"] = task_id
    if surface_path is not None:
        payload["surface_path"] = surface_path
    if stdout_path is not None:
        payload["stdout_path"] = stdout_path
    if worktree_path is not None:
        payload["worktree_path"] = worktree_path
    try:
        _atomic_write_json(path, payload)
    except OSError as exc:
        log.warning(
            "failed to write worker dispatch marker for %s at %s: %s",
            bg_id,
            path,
            exc,
        )
        return None
    return path


def write_done_marker(
    mind_dir: Optional[pathlib.Path],
    *,
    bg_id: str,
    outcome: str,
    pr_url: Optional[str] = None,
    commit_sha: Optional[str] = None,
    finished_at: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[pathlib.Path]:
    """Drop ``inner/notes/worker-<bg_id>-done.json``.

    ``outcome`` is one of ``pr_opened``, ``no_change``, ``error``,
    ``timeout``, or anything the completion handler decides is
    informative — the derive pass stores the string verbatim.
    """
    notes = _notes_dir(mind_dir)
    if notes is None:
        return None
    path = notes / f"worker-{bg_id}-done.json"
    payload: dict[str, Any] = {
        "tag": "worker-completed",
        "bg_id": bg_id,
        "outcome": outcome,
        "finished_at": finished_at or _utc_now_iso(),
    }
    if pr_url is not None:
        payload["pr_url"] = pr_url
    if commit_sha is not None:
        payload["commit_sha"] = commit_sha
    if error is not None:
        payload["error"] = error
    try:
        _atomic_write_json(path, payload)
    except OSError as exc:
        log.warning(
            "failed to write worker done marker for %s at %s: %s",
            bg_id,
            path,
            exc,
        )
        return None
    return path


# ---------------------------------------------------------------------------
# Field-extraction helpers for the completion handler.
#
# The completion flow only has the sub-agent's final text to look
# at — the structured ``worker_report`` (parsed elsewhere) or the
# raw text. These helpers pull PR URL + commit SHA from either
# shape so the ``-done.json`` marker carries the useful fields
# even when the agent reports via free-form prose.


def pr_url_from_report(report: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not report:
        return None
    pr = report.get("pr_url")
    if isinstance(pr, str) and pr.strip():
        return pr.strip()
    return None


def commit_sha_from_report(report: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not report:
        return None
    sha = report.get("commit_sha")
    if isinstance(sha, str) and sha.strip():
        return sha.strip()
    return None


def outcome_from_report(
    report: Optional[Mapping[str, Any]], *, is_error: bool
) -> str:
    """Derive an outcome label from the REQUIRED-REPORT or error flag.

    Rules:

    * ``is_error=True`` → ``error``.
    * report has ``pr_url`` → ``pr_opened``.
    * report has ``kind=research`` (no code shipped) → ``research``.
    * otherwise → ``no_change`` (the agent ran but produced nothing
      observable — the 2026-11-06 ``bg-ceae9c728506`` case).
    """
    if is_error:
        return "error"
    if report is None:
        return "no_change"
    if pr_url_from_report(report):
        return "pr_opened"
    kind = report.get("kind")
    if isinstance(kind, str) and kind.lower() == "research":
        return "research"
    return "no_change"
