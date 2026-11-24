"""Steerable sub-agent dispatch helper (design §7.4).

Design: ``cortex-memory/designs/2026-11-24-subagent-driven-running-now.md``.

A single-call-site replacement for the "in-process ``Agent(...)`` tool" shape
used across Alice's dispatchers. The helper:

1. Mints (or reuses) an SM v2 task row via the ``task`` CLI — cascading
   ``draft → selected → building`` the same way the PreToolUse Agent hook does.
2. Spawns a **detached headless Claude Code session** via
   ``claude --print --session-id <uuid> --remote-control task-NNNN --name ...``
   (same shape the forge dispatcher already uses). Remote Control makes the
   session discoverable to Speaking's ``SendMessage`` tool so Jason can steer
   it mid-flight.
3. Appends a start row to ``/state/worker/alice-task-dispatch.jsonl`` (the
   PreToolUse Agent hook doesn't fire for subprocess spawns; the helper is
   the start-row writer for those).
4. Returns a :class:`SteeringHandle` whose ``.send(msg)`` is the stable
   steering primitive — thin wrapper that drops a surface or (eventually)
   calls ``SendMessage`` from inside a Speaking turn.

Stdlib-only. Any failure in the task-row cascade is non-fatal — the dispatch
still proceeds, just without the SM breadcrumb (matching the hook's
best-effort contract).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import pathlib
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

# Pinned paths — mirror the PreToolUse hook (`~/.claude/hooks/pretooluse-agent-task.py`)
# and the orphan reaper (`src/hooks/orphan_reaper.py`) so start/stop
# events land in the same jsonl pair the status-site reads.
DISPATCH_LOG = pathlib.Path("/state/worker/alice-task-dispatch.jsonl")

# Resolve the task CLI. The container bakes it at /home/alice/alice/bin/task;
# the shell shim sets PYTHONPATH and execs ``python -m alice_forge.task_cli``.
# Callers can override via the ``TASK_BIN`` env var for tests.
_DEFAULT_TASK_BIN = "/home/alice/alice/bin/task"


# Agent dispatches we never make steerable — mirrors the PreToolUse hook's
# SKIP_SUBAGENT_TYPES. ``fork`` is context-management (no artifact);
# ``Explore`` / ``Plan`` / ``statusline-setup`` / ``web-fetch`` are read-only.
# Callers in the dispatcher sources still invoke the helper; the helper
# short-circuits for these types and returns a pass-through handle.
SKIP_STEERABLE_TYPES = frozenset(
    {"fork", "Explore", "Plan", "statusline-setup", "web-fetch"}
)


@dataclass(frozen=True)
class SteeringHandle:
    """Stable return shape — the dispatcher's steering primitive.

    Attributes
    ----------
    task_id:
        The SM v2 task slug (``task-NNNN``). ``None`` iff the task cascade
        failed (helper logged the failure; dispatch still proceeded). The
        caller should treat a ``None`` task_id as "no steering, no row in
        Running now" — not an error.
    session_id:
        UUID pinned on the detached ``claude --print`` process so stdout /
        session-file lookups match forge's reconciliation.
    pid:
        OS pid of the spawned worker. ``None`` if the helper didn't spawn
        (e.g. ``dry_run`` or a skipped subagent type).
    session_name:
        The ``--remote-control``/``--name`` label. Always ``task_id`` when
        present, so ``SendMessage(to=session_name, ...)`` is the steering
        primitive. ``None`` when ``task_id`` is ``None``.
    steerable:
        True iff a Remote Control session was requested on spawn. Readers
        (status-site, orphan reaper) treat missing/absent as ``False``.
    """

    task_id: str | None
    session_id: str | None
    pid: int | None
    session_name: str | None
    steerable: bool

    def send(self, message: str) -> dict[str, Any]:
        """Send a steer message to this subagent.

        Thin wrapper over the status-site's ``/api/steer`` surface-drop
        pattern — writes a ``steer-pending`` row into the dispatch log for
        Speaking's steer-consumer loop to pick up and route through
        ``SendMessage(to=task_id, ...)``.

        Returning a plain dict (``{ok, error?}``) keeps this callable from
        CLI / tests without an asyncio context.
        """
        if not self.steerable or not self.session_name:
            return {"ok": False, "error": "handle is not steerable"}
        try:
            _append_dispatch_row(
                {
                    "ts": time.time(),
                    "event": "steer_requested",
                    "task_id": self.task_id,
                    "session_name": self.session_name,
                    "message": message,
                }
            )
            return {"ok": True, "task_id": self.task_id}
        except OSError as exc:
            return {"ok": False, "error": f"log write failed: {exc}"}


# --------------------------------------------------------------------------- #
# Dispatch-log writer — flock-serialised to co-habit with the PreToolUse hook.
# --------------------------------------------------------------------------- #


def _append_dispatch_row(row: Mapping[str, Any]) -> None:
    """Append ``row`` to the task-dispatch jsonl under ``fcntl.flock``.

    The PreToolUse hook writes to the same file. Both acquire ``LOCK_EX`` so
    interleaved writes can't tear each other's JSON. Non-fatal on write
    failure: the caller catches ``OSError`` and logs.
    """
    DISPATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCH_LOG.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(row) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------- #
# Shared SM-task cascade — factor the ``draft → selected → building`` triad out
# of the PreToolUse hook so the helper + hook speak the same contract.
# --------------------------------------------------------------------------- #


def _run_task_cli(
    bin_path: str,
    args: Sequence[str],
    *,
    timeout: float = 10.0,
) -> dict | None:
    """Invoke ``task --json <args>`` and return the parsed first line.

    Returns ``None`` on any failure. The CLI emits NDJSON (one object per
    line); callers that just want the create response take the first line.
    """
    try:
        result = subprocess.run(
            [bin_path, "--json", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("task CLI exec failed: %s", exc)
        return None
    if result.returncode != 0:
        log.warning(
            "task CLI exit %s: %s",
            result.returncode,
            result.stderr.strip() or "(no stderr)",
        )
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    first_line = out.splitlines()[0]
    try:
        return json.loads(first_line)
    except json.JSONDecodeError:
        return None


def mint_task_row(
    title: str,
    *,
    actor: str = "speaking",
    artifact_type: str = "code",
    source: str = "speaking",
    tags: Sequence[str] = (),
    reason: str = "steerable dispatch — minted by Speaking",
    bin_path: str | None = None,
) -> str | None:
    """Create a new SM v2 task row and transition it ``draft → building``.

    Returns the task id (``task-NNNN``) on success, or ``None`` if any step
    failed (logged, non-fatal — the dispatcher continues without a task).

    This is the factored-out implementation of the cascade the PreToolUse
    Agent hook runs today. Callers that already have a task id (e.g. the
    dispatcher spawning against an existing SM row) skip the mint and pass
    the id straight to :func:`dispatch_steerable_subagent`.
    """
    bin_path = bin_path or os.environ.get("TASK_BIN") or _DEFAULT_TASK_BIN

    tags_csv = ",".join(t for t in tags if t)
    create_args = [
        "create",
        "--title", title[:200],
        "--actor", actor,
        "--artifact-type", artifact_type,
        "--source", source,
        "--reason", reason,
    ]
    if tags_csv:
        create_args.extend(["--tags", tags_csv])

    created = _run_task_cli(bin_path, create_args)
    if not created or "id" not in created:
        log.warning("task create returned no id for title=%r", title)
        return None
    task_id = created["id"]

    # draft → selected → building. Per-transition failures are non-fatal;
    # we log and continue so a partial cascade still leaves a row that the
    # status-site can render (even as stuck-in-selected).
    for status, note in (
        ("selected", "scope confirmed by Speaking at dispatch"),
        ("building", "steerable sub-agent dispatched"),
    ):
        updated = _run_task_cli(
            bin_path,
            [
                "update",
                task_id,
                "--status", status,
                "--actor", actor,
                "--reason", note,
            ],
        )
        if updated is None:
            log.warning(
                "task %s: transition → %s returned no payload", task_id, status
            )
    return task_id


# --------------------------------------------------------------------------- #
# Spawn helpers — modelled on forge's `spawn.py:940` detach pattern.
# --------------------------------------------------------------------------- #


def _build_claude_argv(
    *,
    session_id: str,
    task_id: str | None,
    name: str,
    worktree: pathlib.Path | None,
    model: str | None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Assemble the ``claude --print --remote-control ...`` command line.

    Shape follows forge's detach — ``--print`` for non-interactive output,
    ``--session-id`` to pin the JSONL path for stdout reconciliation,
    ``--remote-control`` + ``--name`` for discovery via ``SendMessage``.

    ``task_id`` doubles as the Remote Control label. When absent (task
    cascade failed), we fall back to a bare ``--name`` and skip
    ``--remote-control`` — the resulting session is still a valid detached
    worker, just non-steerable (which the handle reflects).
    """
    argv: list[str] = ["claude", "--print", "--session-id", session_id, "--name", name]
    if task_id:
        argv.extend(["--remote-control", task_id])
    if worktree is not None:
        argv.extend(["--worktree", str(worktree)])
    if model:
        argv.extend(["--model", model])
    argv.extend(extra_args)
    return argv


def _spawn_detached(
    argv: Sequence[str],
    *,
    prompt: str,
    scratch_dir: pathlib.Path,
) -> int | None:
    """Launch a detached ``claude`` process. Returns its pid or None.

    Mirrors ``alice_bug_fix_dispatcher.main._spawn_claude_detached`` but
    pipes ``prompt`` on stdin (the ``--print`` contract reads the prompt
    from stdin when no argv prompt is passed). ``start_new_session=True``
    decouples the child from the dispatcher's process group so an s6
    restart doesn't SIGHUP the worker.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = scratch_dir / "result.json"
    stderr_path = scratch_dir / "stderr.log"
    try:
        stdout_f = open(stdout_path, "wb")
        stderr_f = open(stderr_path, "wb")
    except OSError as exc:
        log.warning("steerable spawn: open fds failed: %s", exc)
        return None

    # Prompt is passed via stdin so the argv stays clean (no huge argv entry
    # blowing up /proc/<pid>/cmdline, and any `[auto-task]` sentinel the
    # caller already injected lands verbatim on stdin).
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=stdout_f,
            stderr=stderr_f,
            start_new_session=True,
            close_fds=True,
            cwd=str(scratch_dir),
        )
    except OSError as exc:
        log.warning("steerable spawn: Popen failed: %s", exc)
        try:
            stdout_f.close()
            stderr_f.close()
        except OSError:
            pass
        return None
    finally:
        pass

    try:
        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
            finally:
                proc.stdin.close()
    except (OSError, BrokenPipeError) as exc:
        # Prompt write failed — the child is already in flight but will
        # read EOF on stdin. Let the orphan reaper clean it up if it
        # hangs. Don't tear it down from under the caller.
        log.warning("steerable spawn: prompt write failed: %s", exc)
    finally:
        # Parent's copies of the fds can close — the child inherited them.
        try:
            stdout_f.close()
            stderr_f.close()
        except OSError:
            pass
    return proc.pid


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def dispatch_steerable_subagent(
    *,
    task_id: str | None,
    prompt: str,
    description: str = "",
    title: str | None = None,
    worktree: pathlib.Path | None = None,
    subagent_type: str = "claude",
    model: str | None = None,
    tags: Sequence[str] = (),
    scratch_dir: pathlib.Path | None = None,
    extra_claude_args: Sequence[str] = (),
    dry_run: bool = False,
) -> SteeringHandle:
    """Spawn a steerable detached Claude Code sub-agent (design §7.4).

    Parameters
    ----------
    task_id:
        Existing SM task slug. Pass ``None`` to have the helper mint a new
        row via the ``task`` CLI (cascades ``draft → selected → building``).
    prompt:
        The full prompt for the sub-agent. Written to the child's stdin.
    description:
        Short label for the Running-now row and dispatch-log entry.
    title:
        Title for the SM task row (defaults to ``description``).
    worktree, model:
        Forwarded to ``claude --worktree`` / ``--model``.
    subagent_type:
        Metadata only — recorded on the dispatch row. See
        :data:`SKIP_STEERABLE_TYPES` for types that bypass steerability.
    tags:
        Tags written to the SM task row + dispatch log.
    scratch_dir:
        Where stdout / stderr land. Defaults to a per-dispatch tmp dir.
    extra_claude_args:
        Appended verbatim to the ``claude`` argv (escape-hatch for callers
        that need a flag the helper doesn't surface — e.g. ``--add-dir``).
    dry_run:
        When True, skip ``subprocess.Popen`` and ``SM task`` writes; just
        return the handle shape the caller would have seen. Tests only.

    Returns
    -------
    :class:`SteeringHandle` with the start-row details. The helper has
    already written the ``{"steerable": true, "session_name": ...}``
    start row to the dispatch log by the time it returns.
    """
    session_id = uuid.uuid4().hex
    title = title or description or "steerable dispatch"

    # Skip-list short-circuit: the caller invoked the helper for a type
    # we never make steerable. Return a pass-through handle — no task
    # row, no spawn, no log row. The dispatcher keeps the in-process
    # ``Agent(...)`` path for those types.
    if subagent_type in SKIP_STEERABLE_TYPES:
        return SteeringHandle(
            task_id=None,
            session_id=session_id,
            pid=None,
            session_name=None,
            steerable=False,
        )

    if dry_run:
        return SteeringHandle(
            task_id=task_id or "task-DRYRUN",
            session_id=session_id,
            pid=None,
            session_name=(task_id or "task-DRYRUN"),
            steerable=True,
        )

    # 1. Resolve task_id — either reuse the caller's or mint a new one.
    if task_id is None:
        task_id = mint_task_row(
            title=title,
            actor="speaking",
            artifact_type="code",
            source="speaking",
            tags=tuple(tags) + ("auto-registered", "steerable"),
            reason="steerable dispatch — minted by Speaking",
        )
        # mint_task_row logs on failure; proceed without steerability.

    steerable = task_id is not None
    session_name = task_id if steerable else None

    # 2. Build argv + spawn.
    argv = _build_claude_argv(
        session_id=session_id,
        task_id=task_id,
        name=f"{task_id} {title}"[:120] if task_id else title[:120],
        worktree=worktree,
        model=model,
        extra_args=extra_claude_args,
    )
    scratch_dir = scratch_dir or pathlib.Path(
        f"/state/worker/steerable-{session_id[:8]}"
    )
    pid = _spawn_detached(argv, prompt=prompt, scratch_dir=scratch_dir)

    # 3. Write start row — matching the PreToolUse hook's schema plus
    # the §7.5 additions (``session_name`` + ``steerable``).
    try:
        _append_dispatch_row(
            {
                "ts": time.time(),
                "task_id": task_id,
                # No real tool_call_id for subprocess spawns — use the
                # session_id as the primary key. The orphan reaper reads
                # this field as the start/stop join key.
                "tool_call_id": session_id,
                "session_id": session_id,
                "subagent_type": subagent_type,
                "description": description,
                "title": title,
                "tags": list(tags),
                "session_name": session_name,
                "steerable": steerable,
                "pid": pid,
            }
        )
    except OSError as exc:
        log.warning("steerable dispatch: start-row write failed: %s", exc)

    return SteeringHandle(
        task_id=task_id,
        session_id=session_id,
        pid=pid,
        session_name=session_name,
        steerable=steerable,
    )
