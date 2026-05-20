"""ExperimentRunner — async hand-off to a sandboxed ``claude`` CLI subagent.

The runner is the engine behind the ``run_experiment`` MCP tool. It:

1. Generates a stable experiment_id (``exp-YYYY-MM-DD-HHMMSS-<6-char-hex>``).
2. Optionally rsyncs ``/home/alice/alice/`` into
   ``/tmp/alice-copy-<experiment_id>/`` when ``repo_under_test`` is set.
3. Generates a permission-rules file (deny-list per :mod:`.permissions`).
4. Writes an MCP-config file pointing at the stdio ``submit_result`` server.
5. Subprocesses ``claude`` with ``--print``, the synthesized instructions,
   the rules file via ``--settings``, the MCP config, and the appropriate
   ``--add-dir`` flags.
6. Streams stdout/stderr to ``inner/state/experiments/<id>/transcript.jsonl``
   one event per line.
7. On subprocess exit, polls the status sidecar to determine whether
   ``submit_result`` was called. Writes a failed-stub card if not.
8. Emits the surface note, viewer event, and jsonl line, then GCs the
   ephemeral ``/tmp/alice-copy-<id>/`` + settings/MCP-config files.

The runner is fire-and-walk: :meth:`dispatch` returns the :class:`DispatchMetadata`
immediately, schedules the subprocess as an asyncio task, and lets the
parent wake close. The asyncio task carries the side effects through to
completion.

Wall-clock safety net: a configurable timeout (default 30 min) wraps the
subprocess so a runaway subagent doesn't burn the host. On timeout the
runner kills the subprocess, writes a failed-stub card with
``failure_reason: timeout``, and emits all three side effects.

The runner is also constructed with an :class:`EventEmitter` (typically the
wake-side :class:`EventLogger`) so dispatches show up in
``/state/worker/thinking.log`` and the viewer can render the events.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import json
import logging
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Optional

from core.events import EventEmitter

from .card import (
    DEFAULT_VAULT_EXPERIMENTS_DIR,
    card_path_for,
    write_failed_stub_card,
)
from .permissions import generate_permission_rules
from .submit_result import STATUS_FILE_NAME
from .surface import (
    DEFAULT_EXPERIMENTS_JSONL,
    DEFAULT_SURFACE_DIR,
    append_experiments_jsonl,
    emit_completion_event,
    write_surface_note,
)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DispatchMetadata",
    "ExperimentDispatchError",
    "ExperimentOutcome",
    "ExperimentRunner",
    "UnknownExperimentError",
    "new_experiment_id",
]


log = logging.getLogger(__name__)


# Candidate interpreters for the ``submit_result`` MCP server. Order matters
# — first one that imports ``claude_agent_sdk`` cleanly wins. ``sys.executable``
# is checked implicitly inside the picker below before the fallbacks.
_MCP_PYTHON_FALLBACKS: tuple[str, ...] = (
    "/opt/alice-venv/bin/python3",
    "/host-home/alice-speaking/.venv/bin/python",
)


def _pick_mcp_python() -> str:
    """Return a python that has ``claude_agent_sdk`` importable.

    The MCP server is a tiny stdio process; we just need an interpreter
    that can run ``alice_thinking.experiments.submit_result`` without
    blowing up on the SDK import. Falls back to ``sys.executable`` if no
    fallback path passes — the runner will still try, and the failure
    will surface as a clear traceback in the transcript.
    """
    candidates: tuple[str, ...] = (sys.executable, *_MCP_PYTHON_FALLBACKS)
    for path in candidates:
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, "-c", "import claude_agent_sdk"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return path
    return sys.executable


# Wall-clock safety net per the design-review concern. Not a "kill after"
# expectation — most experiments finish in seconds. This catches runaway
# loops the deny-list doesn't (e.g. read→write→read in a tight cycle).
# 30 minutes is the review's recommendation; configurable per dispatch.
DEFAULT_TIMEOUT_SECONDS = 30 * 60


# Default subagent model. The subagent gets a small, cheap model by default
# because experiments are usually a focused single-task workload. Overridable
# via the dispatch ``model`` argument.
DEFAULT_SUBAGENT_MODEL = "claude-sonnet-4-6"


# Source repo for the writable-copy rsync. Hard-coded because the spec
# names it explicitly; the alternative would be a configurable list but
# v1 only supports one ``repo_under_test`` value (``"alice"``).
DEFAULT_REPO_SOURCE = pathlib.Path("/home/alice/alice")


# Filesystem prefixes the runner manages under /tmp. Tracked here so the
# GC is honest about what it can remove and tests can override the parent.
DEFAULT_TMP_ROOT = pathlib.Path("/tmp")


class ExperimentDispatchError(RuntimeError):
    """Raised when :meth:`ExperimentRunner.dispatch` cannot accept the input.

    Per the v2 spec ``method`` and ``inline_instructions`` are XOR — passing
    both, neither, or an unreadable method path raises before any work
    starts.
    """


class UnknownExperimentError(LookupError):
    """Raised by :meth:`ExperimentRunner.wait_for` for an unknown experiment_id.

    "Unknown" means this runner instance never dispatched the id. Mostly a
    safety net for the CLI: a typo at the shell shouldn't silently succeed.
    """


@dataclasses.dataclass(frozen=True)
class ExperimentOutcome:
    """Terminal state of one experiment, derived from the on-disk card.

    The runner builds one of these after :meth:`ExperimentRunner.wait_for`
    drains the asyncio task. All fields except ``experiment_id`` /
    ``card_path`` / ``status`` are best-effort: a missing card or a card
    written before the schema settled leaves the corresponding field as
    ``None`` / ``""`` rather than raising. The CLI uses ``status`` to set
    its process exit code and ``summary`` for the JSON payload it prints.
    """

    experiment_id: str
    card_path: pathlib.Path
    status: str  # "complete" | "incomplete" | "failed" | "missing-card"
    summary: str  # the card's Abstract body (or stub message)
    hypothesis: str
    dispatched_at: Optional[datetime.datetime]
    completed_at: Optional[datetime.datetime]
    duration_seconds: Optional[float]
    transcript_path: Optional[pathlib.Path]
    failure_reason: Optional[str]

    def to_json_payload(self) -> dict[str, Any]:
        """Render the dict the CLI prints to stdout.

        Strings everywhere — datetimes go ISO-8601, paths go str(), Nones
        are kept so consumers can distinguish "no value" from "missing
        key" without re-parsing the card frontmatter.
        """
        return {
            "experiment_id": self.experiment_id,
            "card_path": str(self.card_path),
            "status": self.status,
            "summary": self.summary,
            "hypothesis": self.hypothesis,
            "dispatched_at": _iso_or_none(self.dispatched_at),
            "completed_at": _iso_or_none(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "transcript_path": str(self.transcript_path)
            if self.transcript_path is not None
            else None,
            "failure_reason": self.failure_reason,
        }


def _iso_or_none(dt: Optional[datetime.datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0).isoformat()


@dataclasses.dataclass(frozen=True)
class DispatchMetadata:
    """The dict the ``run_experiment`` MCP tool returns immediately."""

    experiment_id: str
    card_path: pathlib.Path
    transcript_path: pathlib.Path
    dispatched_at: datetime.datetime
    status: str = "dispatched"

    def to_tool_response(self) -> dict[str, Any]:
        """Render the spec'd return dict (paths as strings, ISO ts)."""
        return {
            "experiment_id": self.experiment_id,
            "status": self.status,
            "card_path": str(self.card_path),
            "transcript_path": str(self.transcript_path),
            "dispatched_at": self.dispatched_at.replace(microsecond=0).isoformat(),
        }


def new_experiment_id(now: Optional[datetime.datetime] = None) -> str:
    """Generate a stable experiment_id.

    Format: ``exp-YYYY-MM-DD-HHMMSS-<6-char-hex>``. The hex suffix is
    cryptographically random so back-to-back dispatches within the same
    second don't collide. The 6 chars give us 2^24 ≈ 16M values per
    second-bucket — plenty of headroom.
    """
    if now is None:
        now = datetime.datetime.now().astimezone()
    timestamp = now.strftime("%Y-%m-%d-%H%M%S")
    suffix = secrets.token_hex(3)  # 3 bytes = 6 hex chars
    return f"exp-{timestamp}-{suffix}"


# ---------------------------------------------------------------------------
# The runner.


class ExperimentRunner:
    """Coordinates the async dispatch + completion lifecycle.

    One instance per wake. ``dispatch(...)`` schedules a subprocess in
    the asyncio loop and returns metadata immediately; the side effects
    (card / surface / event / jsonl / GC) happen in the background task.
    """

    def __init__(
        self,
        emitter: EventEmitter,
        *,
        vault_experiments_dir: pathlib.Path = DEFAULT_VAULT_EXPERIMENTS_DIR,
        surface_dir: pathlib.Path = DEFAULT_SURFACE_DIR,
        experiments_jsonl: pathlib.Path = DEFAULT_EXPERIMENTS_JSONL,
        state_dir: pathlib.Path = pathlib.Path(
            "/home/alice/alice-mind/inner/state/experiments"
        ),
        tmp_root: pathlib.Path = DEFAULT_TMP_ROOT,
        repo_source: pathlib.Path = DEFAULT_REPO_SOURCE,
        default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        default_model: str = DEFAULT_SUBAGENT_MODEL,
        api_key: Optional[str] = None,
        api_base_url: Optional[str] = None,
        claude_binary: str = "claude",
        # Hook for tests: replace the subprocess invocation entirely.
        subprocess_runner: Optional[
            Callable[..., "asyncio.subprocess.Process"]
        ] = None,
    ) -> None:
        self.emitter = emitter
        self.vault_experiments_dir = vault_experiments_dir
        self.surface_dir = surface_dir
        self.experiments_jsonl = experiments_jsonl
        self.state_dir = state_dir
        self.tmp_root = tmp_root
        self.repo_source = repo_source
        self.default_timeout_seconds = default_timeout_seconds
        self.default_model = default_model
        self.api_key = api_key
        self.api_base_url = api_base_url
        self.claude_binary = claude_binary
        self._subprocess_runner = subprocess_runner
        # Track live tasks so callers can drain them at shutdown if
        # needed. Each entry removes itself on completion.
        self._tasks: dict[str, asyncio.Task] = {}
        # Track every experiment_id this runner has dispatched so
        # :meth:`wait_for` can tell "task already finished and was
        # untracked" apart from "id never seen." Survives across the
        # done-callback that prunes ``_tasks``.
        self._dispatched: dict[str, DispatchMetadata] = {}

    # ------------------------------------------------------------------
    # Public API

    def dispatch(
        self,
        *,
        hypothesis: str,
        method: Optional[str],
        inline_instructions: Optional[str],
        expected_output: str,
        context_paths: Optional[list[str]] = None,
        repo_under_test: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        model: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
    ) -> DispatchMetadata:
        """Schedule one experiment. Returns metadata immediately.

        Validates the XOR between ``method`` and ``inline_instructions``,
        materializes the experiment_id + per-experiment paths, kicks off
        the asyncio task, and returns. The task carries the lifecycle
        through to side-effect emission and GC.
        """
        if not isinstance(hypothesis, str) or not hypothesis.strip():
            raise ExperimentDispatchError("hypothesis must be a non-empty string")
        if (method is None) == (inline_instructions is None):
            raise ExperimentDispatchError(
                "exactly one of `method` (script path) or `inline_instructions` "
                "(literal text) must be set"
            )
        if not isinstance(expected_output, str) or not expected_output.strip():
            raise ExperimentDispatchError("expected_output must be a non-empty string")
        if context_paths is not None:
            if not isinstance(context_paths, list) or not all(
                isinstance(p, str) for p in context_paths
            ):
                raise ExperimentDispatchError(
                    "context_paths must be a list of strings (if provided)"
                )

        method_text: Optional[str] = None
        if method is not None:
            method_path = pathlib.Path(method)
            if not method_path.is_file():
                raise ExperimentDispatchError(
                    f"method path not found or not a file: {method}"
                )
            try:
                method_text = method_path.read_text()
            except OSError as exc:
                raise ExperimentDispatchError(
                    f"could not read method file {method}: {exc}"
                ) from exc

        dispatched_at = now or datetime.datetime.now().astimezone()
        experiment_id = new_experiment_id(dispatched_at)
        per_experiment_state = self.state_dir / experiment_id
        per_experiment_state.mkdir(parents=True, exist_ok=True)
        transcript_path = per_experiment_state / "transcript.jsonl"
        # Pre-create the empty transcript so callers can stat / tail it
        # before the subprocess writes its first event.
        transcript_path.touch()
        card_path = card_path_for(
            experiment_id, vault_path=self.vault_experiments_dir
        )

        meta = DispatchMetadata(
            experiment_id=experiment_id,
            card_path=card_path,
            transcript_path=transcript_path,
            dispatched_at=dispatched_at,
        )
        self._dispatched[experiment_id] = meta

        # Emit dispatch telemetry before scheduling so the viewer shows
        # the experiment in-flight even if the background task hasn't
        # actually started yet.
        try:
            self.emitter.emit(
                "experiment_dispatch",
                experiment_id=experiment_id,
                hypothesis=hypothesis,
                expected_output=expected_output,
                repo_under_test=repo_under_test,
                card_path=str(card_path),
                transcript_path=str(transcript_path),
            )
        except Exception:  # noqa: BLE001
            log.exception("emit experiment_dispatch failed for %s", experiment_id)

        # Schedule the background task. We don't await — the spec is
        # explicit that dispatch returns immediately.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — the caller is sync (e.g. a test). Run
            # the side effects synchronously via asyncio.run; this is
            # not the production path but lets unit tests poke at it
            # without setting up a loop.
            asyncio.run(
                self._run_experiment(
                    meta=meta,
                    hypothesis=hypothesis,
                    method_text=method_text,
                    inline_instructions=inline_instructions,
                    expected_output=expected_output,
                    context_paths=context_paths or [],
                    repo_under_test=repo_under_test,
                    timeout_seconds=timeout_seconds or self.default_timeout_seconds,
                    model=model or self.default_model,
                )
            )
            return meta

        task = loop.create_task(
            self._run_experiment(
                meta=meta,
                hypothesis=hypothesis,
                method_text=method_text,
                inline_instructions=inline_instructions,
                expected_output=expected_output,
                context_paths=context_paths or [],
                repo_under_test=repo_under_test,
                timeout_seconds=timeout_seconds or self.default_timeout_seconds,
                model=model or self.default_model,
            ),
            name=f"experiment-{experiment_id}",
        )

        def _untrack(_task: asyncio.Task) -> None:
            self._tasks.pop(experiment_id, None)

        task.add_done_callback(_untrack)
        self._tasks[experiment_id] = task
        return meta

    async def wait_for(
        self,
        experiment_id: str,
        timeout: Optional[float] = None,
    ) -> ExperimentOutcome:
        """Await one in-flight experiment task; return its terminal outcome.

        Used by the synchronous ``alice-experiment`` CLI (and by tests).
        The MCP-tool path doesn't wait — it dispatches and ends the wake.

        Behaviour:

        - If the id was never dispatched through this runner, raise
          :class:`UnknownExperimentError` so a CLI typo doesn't return a
          fake "missing-card" outcome.
        - If the task is still in flight, await it. ``asyncio.shield``
          wraps the underlying task so a CLI-level cancel doesn't kill
          the background work (the side effects must finish; the card
          must land).
        - If the task already completed (and was pruned from
          ``_tasks`` by the done-callback), build the outcome straight
          from disk — the side effects have already landed.

        ``timeout`` is the wait-for timeout in seconds; on expiry
        :class:`asyncio.TimeoutError` propagates so the caller can map it
        to an exit code. The underlying task keeps running — the CLI
        chooses what to do with that.
        """
        if experiment_id not in self._dispatched:
            raise UnknownExperimentError(
                f"experiment_id {experiment_id!r} was not dispatched by this runner"
            )
        task = self._tasks.get(experiment_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self._build_outcome(experiment_id)

    def _build_outcome(self, experiment_id: str) -> ExperimentOutcome:
        """Construct an :class:`ExperimentOutcome` from the on-disk card.

        Falls back to dispatch metadata when the card is missing or
        unreadable — better to surface a ``missing-card`` outcome than
        to crash the caller after a long wait.
        """
        meta = self._dispatched[experiment_id]
        card_path = meta.card_path
        if not card_path.is_file():
            return ExperimentOutcome(
                experiment_id=experiment_id,
                card_path=card_path,
                status="missing-card",
                summary="",
                hypothesis="",
                dispatched_at=meta.dispatched_at,
                completed_at=None,
                duration_seconds=None,
                transcript_path=meta.transcript_path,
                failure_reason="card file was not written",
            )
        try:
            text = card_path.read_text()
        except OSError as exc:
            return ExperimentOutcome(
                experiment_id=experiment_id,
                card_path=card_path,
                status="missing-card",
                summary="",
                hypothesis="",
                dispatched_at=meta.dispatched_at,
                completed_at=None,
                duration_seconds=None,
                transcript_path=meta.transcript_path,
                failure_reason=f"could not read card: {type(exc).__name__}: {exc}",
            )
        fm = _parse_frontmatter(text)
        status = fm.get("status") or "complete"
        summary = self._extract_abstract_from_card(card_path)
        return ExperimentOutcome(
            experiment_id=experiment_id,
            card_path=card_path,
            status=status,
            summary=summary,
            hypothesis=fm.get("hypothesis") or "",
            dispatched_at=_parse_iso(fm.get("dispatched_at"))
            or meta.dispatched_at,
            completed_at=_parse_iso(fm.get("completed_at")),
            duration_seconds=_parse_float(fm.get("duration_seconds")),
            transcript_path=meta.transcript_path,
            failure_reason=fm.get("failure_reason") or None,
        )

    # ------------------------------------------------------------------
    # Background task — runs the subagent + emits side effects.

    async def _run_experiment(
        self,
        *,
        meta: DispatchMetadata,
        hypothesis: str,
        method_text: Optional[str],
        inline_instructions: Optional[str],
        expected_output: str,
        context_paths: list[str],
        repo_under_test: Optional[str],
        timeout_seconds: int,
        model: str,
    ) -> None:
        """Drive one subagent end-to-end. All side effects happen here."""
        experiment_id = meta.experiment_id
        card_path = meta.card_path
        transcript_path = meta.transcript_path
        dispatched_at = meta.dispatched_at
        per_experiment_state = transcript_path.parent
        status_path = per_experiment_state / STATUS_FILE_NAME
        settings_path = per_experiment_state / "settings.json"
        mcp_config_path = per_experiment_state / "mcp-config.json"

        writable_repo_copy: Optional[pathlib.Path] = None
        if repo_under_test:
            writable_repo_copy = self.tmp_root / f"alice-copy-{experiment_id}"
            await asyncio.to_thread(
                self._provision_repo_copy, writable_repo_copy
            )

        generate_permission_rules(
            settings_path, writable_repo_copy=writable_repo_copy
        )
        self._write_mcp_config(
            mcp_config_path,
            experiment_id=experiment_id,
            card_path=card_path,
            transcript_path=transcript_path,
            status_dir=per_experiment_state,
            dispatched_at=dispatched_at,
            repo_under_test=repo_under_test,
        )

        prompt = self._synthesize_prompt(
            hypothesis=hypothesis,
            method_text=method_text,
            inline_instructions=inline_instructions,
            expected_output=expected_output,
            context_paths=context_paths,
            repo_under_test=repo_under_test,
            writable_repo_copy=writable_repo_copy,
        )

        start_time = time.monotonic()
        timeout_hit = False
        process_returncode: Optional[int] = None
        try:
            try:
                process_returncode = await asyncio.wait_for(
                    self._run_claude_subprocess(
                        prompt=prompt,
                        settings_path=settings_path,
                        mcp_config_path=mcp_config_path,
                        transcript_path=transcript_path,
                        writable_repo_copy=writable_repo_copy,
                        model=model,
                    ),
                    timeout=max(timeout_seconds, 1),
                )
            except asyncio.TimeoutError:
                timeout_hit = True
                log.warning(
                    "experiment %s timed out after %ss",
                    experiment_id,
                    timeout_seconds,
                )
        except Exception:  # noqa: BLE001
            log.exception("experiment %s subprocess raised", experiment_id)
        duration = time.monotonic() - start_time

        completed_at = datetime.datetime.now().astimezone()
        submitted = status_path.is_file()

        # Determine final status.
        status: str
        failure_reason: Optional[str] = None
        if submitted:
            try:
                payload = json.loads(status_path.read_text())
                status = str(payload.get("status") or "complete")
            except (OSError, json.JSONDecodeError):
                status = "complete"
            if status not in ("complete", "incomplete"):
                status = "complete"
        elif timeout_hit:
            status = "failed"
            failure_reason = f"timeout after {timeout_seconds}s"
        elif process_returncode is not None and process_returncode != 0:
            status = "failed"
            failure_reason = f"subagent exited with code {process_returncode}"
        else:
            status = "failed"
            failure_reason = (
                "subagent exited without calling submit_result "
                "(returncode=" + str(process_returncode) + ")"
            )

        # If the subagent didn't write a card, write a stub so thinking
        # always gets an artifact.
        if not card_path.is_file():
            try:
                write_failed_stub_card(
                    card_path,
                    experiment_id=experiment_id,
                    hypothesis=hypothesis,
                    dispatched_at=dispatched_at,
                    completed_at=completed_at,
                    duration_seconds=duration,
                    transcript_path=str(transcript_path),
                    failure_reason=failure_reason or "unknown failure",
                    repo_under_test=repo_under_test,
                )
            except OSError:
                log.exception("failed-stub card write failed for %s", experiment_id)

        # Side effects: surface note, viewer event, jsonl line.
        abstract = self._extract_abstract_from_card(card_path)
        try:
            write_surface_note(
                experiment_id=experiment_id,
                card_path=card_path,
                status=status,
                abstract=abstract,
                surface_dir=self.surface_dir,
                now=completed_at,
            )
        except OSError:
            log.exception("surface note write failed for %s", experiment_id)
        emit_completion_event(
            self.emitter,
            experiment_id=experiment_id,
            card_path=card_path,
            status=status,
            dispatched_at=dispatched_at,
            completed_at=completed_at,
            transcript_path=transcript_path,
            duration_seconds=duration,
            extra={"failure_reason": failure_reason} if failure_reason else None,
        )
        append_experiments_jsonl(
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            dispatched_at=dispatched_at,
            completed_at=completed_at,
            status=status,
            duration_seconds=duration,
            card_path=card_path,
            repo_under_test=repo_under_test,
            jsonl_path=self.experiments_jsonl,
        )

        # GC the writable repo copy.
        if writable_repo_copy is not None:
            try:
                await asyncio.to_thread(
                    shutil.rmtree, writable_repo_copy, ignore_errors=True
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to GC writable repo copy %s", writable_repo_copy
                )

    # ------------------------------------------------------------------
    # Subprocess + filesystem helpers (each one is small + thread-safe)

    def _provision_repo_copy(self, target: pathlib.Path) -> None:
        """Rsync the real repo into ``target``, excluding noisy / sensitive dirs.

        Per the v2 spec: ``rsync -a --exclude=.git /home/alice/alice/ <target>/``.
        We add ``.venv`` and ``__pycache__`` to the exclude list — they
        bloat the copy and the subagent never needs them.

        Falls back to a Python-level ``shutil.copytree`` if rsync isn't
        available (unlikely on the deployed worker; useful for tests).
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        rsync_bin = shutil.which("rsync")
        source = str(self.repo_source).rstrip("/") + "/"
        dest = str(target).rstrip("/") + "/"
        if rsync_bin is not None:
            subprocess.run(
                [
                    rsync_bin,
                    "-a",
                    "--exclude=.git",
                    "--exclude=.venv",
                    "--exclude=__pycache__",
                    "--exclude=node_modules",
                    source,
                    dest,
                ],
                check=False,
            )
            return
        # Fallback: copytree with ignore.
        shutil.copytree(
            self.repo_source,
            target,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "node_modules"),
            dirs_exist_ok=True,
        )

    def _write_mcp_config(
        self,
        target: pathlib.Path,
        *,
        experiment_id: str,
        card_path: pathlib.Path,
        transcript_path: pathlib.Path,
        status_dir: pathlib.Path,
        dispatched_at: datetime.datetime,
        repo_under_test: Optional[str],
    ) -> None:
        """Render the ``--mcp-config`` JSON for the subagent.

        Wires the stdio ``submit_result`` server. The subagent's MCP
        client invokes ``mcp__alice_experiment__submit_result``; the
        CLI auto-routes that to the stdio server we declare here.
        """
        # The CLI's --mcp-config JSON shape: {"mcpServers": {"<name>": {...}}}
        # An stdio server entry is {"type":"stdio","command":"...","args":[...]}.
        #
        # The MCP server needs ``claude_agent_sdk`` importable. ``sys.executable``
        # is the right default when the runner's own python has the SDK, but
        # the runner is also dispatched from environments that intentionally
        # don't (e.g. ``/state/sci-env`` carries torch_geometric for ML
        # experiments but not the SDK). Detect at runtime: prefer
        # ``sys.executable``; otherwise probe a small list of known-good
        # locations and pick the first one that imports the SDK cleanly.
        # That keeps the subagent's MCP server working regardless of which
        # python the operator (or thinking) used to invoke the CLI.
        mcp_python = _pick_mcp_python()
        config = {
            "mcpServers": {
                "alice_experiment": {
                    "type": "stdio",
                    "command": mcp_python,
                    "args": [
                        "-m",
                        "alice_thinking.experiments.submit_result",
                        "--experiment-id",
                        experiment_id,
                        "--card-path",
                        str(card_path),
                        "--status-dir",
                        str(status_dir),
                        "--transcript-path",
                        str(transcript_path),
                        "--dispatched-at",
                        dispatched_at.replace(microsecond=0).isoformat(),
                        "--repo-under-test",
                        repo_under_test or "",
                    ],
                }
            }
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config, indent=2))

    def _synthesize_prompt(
        self,
        *,
        hypothesis: str,
        method_text: Optional[str],
        inline_instructions: Optional[str],
        expected_output: str,
        context_paths: list[str],
        repo_under_test: Optional[str],
        writable_repo_copy: Optional[pathlib.Path],
    ) -> str:
        """Build the prompt the subagent receives.

        Includes the hypothesis, the method (literal or inline), the
        ``expected_output`` shape, the must-read-first context paths,
        the writable-repo-copy path (if any), and the ``submit_result``
        contract. The prompt is plain prose so the subagent doesn't
        have to parse a structured envelope — claude reads it like any
        other user message.
        """
        sections: list[str] = []
        sections.append(
            "You are running as an experiment subagent dispatched by the "
            "thinking hemisphere. You are sandboxed: the permission rules "
            "deny `claude`, `git push`, network egress, and container "
            "operations. Stay inside /tmp, ~/alice-mind, and (if granted) "
            "the writable repo copy. Read-only access to /home/alice/alice."
        )
        sections.append(f"# Hypothesis\n\n{hypothesis.strip()}")
        if method_text is not None:
            sections.append(f"# Method (from script)\n\n{method_text.strip()}")
        if inline_instructions is not None:
            sections.append(
                f"# Method (inline instructions)\n\n{inline_instructions.strip()}"
            )
        sections.append(f"# Expected output\n\n{expected_output.strip()}")
        if context_paths:
            sections.append(
                "# Context paths (must-read-first hint, not access control)\n\n"
                + "\n".join(f"- {p}" for p in context_paths)
            )
        if writable_repo_copy is not None:
            sections.append(
                "# Repo under test\n\n"
                f"A writable copy of the alice repo (originally requested as "
                f"`{repo_under_test}`) has been provisioned at "
                f"`{writable_repo_copy}`. Modify code there, install/run "
                f"tests against the copy. The real repo at "
                "/home/alice/alice/ stays read-only — use it for prior-art "
                "lookups only."
            )
        sections.append(
            "# How to report\n\n"
            "When you have a result (or determine the experiment cannot "
            "complete), call the `submit_result` MCP tool with the full "
            "research-paper-shaped payload: title, abstract, hypothesis, "
            "method, results, discussion, conclusion. The runner picks up "
            "the card and surfaces it to thinking. After calling "
            "`submit_result`, end your turn. DO NOT call `submit_result` "
            "more than once."
        )
        return "\n\n".join(sections)

    async def _run_claude_subprocess(
        self,
        *,
        prompt: str,
        settings_path: pathlib.Path,
        mcp_config_path: pathlib.Path,
        transcript_path: pathlib.Path,
        writable_repo_copy: Optional[pathlib.Path],
        model: str,
    ) -> int:
        """Spawn ``claude --print`` and stream events to the transcript.

        Returns the subprocess returncode. Streaming events line-by-line
        into the transcript means a partial / killed subprocess still
        leaves an inspectable trail.

        Tests inject ``subprocess_runner=...`` at construction to swap
        the actual ``asyncio.create_subprocess_exec`` for a mock.
        """
        # Build the argv. ``--print`` runs non-interactively and prints
        # the final assistant text. ``--output-format=stream-json``
        # streams every event so the transcript is line-by-line JSON.
        args = [
            self.claude_binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--settings",
            str(settings_path),
            "--mcp-config",
            str(mcp_config_path),
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Bash,Read,Write,Edit,Glob,Grep,mcp__alice_experiment__submit_result",
        ]
        if writable_repo_copy is not None:
            args.extend(["--add-dir", str(writable_repo_copy)])
        args.extend(["--add-dir", "/home/alice/alice-mind"])
        args.extend(["--add-dir", "/home/alice/alice"])

        env = os.environ.copy()
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
            # Bare mode forces strict ANTHROPIC_API_KEY auth, no OAuth /
            # keychain. Right model for a sandboxed subagent.
            args.append("--bare")
        if self.api_base_url:
            env["ANTHROPIC_BASE_URL"] = self.api_base_url

        log.info("dispatching subagent: %s", " ".join(args))
        if self._subprocess_runner is not None:
            process = await self._subprocess_runner(args, env=env, prompt=prompt)
        else:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

        await self._stream_to_transcript(process, transcript_path)
        return process.returncode if process.returncode is not None else 0

    async def _stream_to_transcript(
        self,
        process: "asyncio.subprocess.Process",
        transcript_path: pathlib.Path,
    ) -> None:
        """Pipe stdout + stderr to the per-experiment transcript.

        Each line is wrapped into a single JSON object on its own line so
        the transcript is grep + jq friendly per the spec §Transcript.
        ``source`` is ``"stdout"`` or ``"stderr"``; ``text`` carries the
        raw line. If the underlying line was already JSON (the CLI's
        ``stream-json`` output is), we surface it under ``event`` so the
        line is still single-shot JSON without double-encoding.
        """

        async def _pump(reader: Optional[asyncio.StreamReader], source: str) -> None:
            if reader is None:
                return
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                record: dict[str, Any] = {
                    "ts": time.time(),
                    "source": source,
                }
                stripped = line.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        record["event"] = json.loads(stripped)
                    except json.JSONDecodeError:
                        record["text"] = line
                else:
                    record["text"] = line
                try:
                    with transcript_path.open("a") as f:
                        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                except OSError:
                    log.exception("transcript write failed")

        await asyncio.gather(
            _pump(process.stdout, "stdout"),
            _pump(process.stderr, "stderr"),
        )
        with contextlib.suppress(asyncio.CancelledError):
            await process.wait()

    def _extract_abstract_from_card(self, card_path: pathlib.Path) -> str:
        """Pull the abstract section out of the card so the surface note
        can carry it. Falls back to an empty string so the surface
        emitter's default-fill kicks in.
        """
        if not card_path.is_file():
            return ""
        try:
            text = card_path.read_text()
        except OSError:
            return ""
        # Find ``## Abstract`` and take everything up to the next ``## ``.
        start_marker = "## Abstract"
        start = text.find(start_marker)
        if start == -1:
            return ""
        rest = text[start + len(start_marker):]
        next_section = rest.find("\n## ")
        if next_section == -1:
            body = rest
        else:
            body = rest[:next_section]
        return body.strip()


# ---------------------------------------------------------------------------
# Frontmatter helpers used by :meth:`ExperimentRunner._build_outcome`.
# Kept module-level (and minimal) so they're trivially unit-testable and
# don't pull PyYAML into the runtime — the card writer hand-rolls YAML for
# the same reason.


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Crude single-line YAML frontmatter parser.

    The card writer only emits scalar string / number / bool / null lines
    in the frontmatter, so this strips outer double-quotes and ignores
    list-valued lines (which the outcome doesn't need). Anything unparsed
    returns as an empty dict — the caller has to handle missing keys
    anyway.
    """
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    block = text[4:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            # Unescape the writer's ``\\`` and ``\"`` sequences.
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[key] = value
    return out


def _parse_iso(raw: Optional[str]) -> Optional[datetime.datetime]:
    """Parse the writer's ISO-8601 timestamps; return None for ``null`` / empty."""
    if not raw or raw == "null":
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _parse_float(raw: Optional[str]) -> Optional[float]:
    """Parse a numeric frontmatter value; return None for ``null`` / empty."""
    if not raw or raw == "null":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
