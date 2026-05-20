"""``alice-experiment`` — synchronous CLI wrapper around :class:`ExperimentRunner`.

Why this exists
---------------

Thinking's sleep wakes drive on pi-mono (``kernels.pi.kernel.PiKernel``),
which has no MCP support — ``_translate_tools`` at
``src/kernels/pi/kernel.py:104-105`` strips every ``mcp__``-prefixed name
before handing the allowed-tools list to pi. The
``mcp__alice__run_experiment`` tool that thinking's MCP server exposes is
therefore invisible to the local Qwen runtime. Pi-mono won't gain MCP, so
this CLI is the second surface for the same dispatch machinery: thinking
shells out to ``alice-experiment`` from Bash and gets a JSON summary
back.

Why synchronous
---------------

The MCP path is fire-and-walk: ``run_experiment`` returns dispatch
metadata immediately, the wake closes, and thinking picks up the result
on her next wake via the surface watcher. That's the right model when
the caller is an LLM that's already paying the per-wake cost.

The CLI is a different shape. A Bash call inside a wake already blocks
the wake until the command returns. Splitting the CLI into "dispatch
now, fetch later" would mean inventing detached-process bookkeeping,
crash-recovery semantics, and a second poll command — for zero
benefit, because the wake is sitting there waiting anyway. So the CLI
runs the whole lifecycle in-process: dispatch → await the asyncio
task → read the card → print the JSON. The AnthropicKernel-driven
``run_experiment`` MCP path is untouched; this is a parallel surface.

Output
------

stdout is a single one-line JSON object with::

    {
      "experiment_id": "exp-...-...",
      "card_path": "/.../<id>.md",
      "status": "complete | incomplete | failed | missing-card",
      "summary": "<abstract section text>",
      "hypothesis": "<one-line hypothesis>",
      "dispatched_at": "ISO-8601",
      "completed_at": "ISO-8601 | null",
      "duration_seconds": <number | null>,
      "transcript_path": "/.../transcript.jsonl",
      "failure_reason": "<string | null>"
    }

Without ``--json`` the CLI also prints two human-readable lines before
the JSON; with ``--json`` (default) it prints only the JSON.

Exit codes
----------

  0 — success (status == complete or incomplete)
  2 — input validation failure (printed to stderr; usage printed too)
  3 — dispatch-time error (ExperimentDispatchError, etc.)
  4 — wall-clock timeout (the underlying task keeps running)
  5 — subagent reported a failure status, or no card was written
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any, Optional

from core.events import EventEmitter, EventLogger

from ..experiments import (
    ExperimentDispatchError,
    ExperimentOutcome,
    ExperimentRunner,
    UnknownExperimentError,
)


__all__ = [
    "EXIT_OK",
    "EXIT_VALIDATION_ERROR",
    "EXIT_DISPATCH_ERROR",
    "EXIT_TIMEOUT",
    "EXIT_SUBAGENT_FAILURE",
    "build_arg_parser",
    "main",
    "run",
]


# Exit codes — kept module-level constants so the test suite can reference
# them by name and the CLI's behaviour is grep-able from the docstring.
EXIT_OK = 0
EXIT_VALIDATION_ERROR = 2
EXIT_DISPATCH_ERROR = 3
EXIT_TIMEOUT = 4
EXIT_SUBAGENT_FAILURE = 5


# Default log destination — matches the production thinking wake
# (``alice_thinking.wake.DEFAULT_LOG``) so CLI-driven dispatches show up
# in the same event stream as MCP-driven ones. Overridable via
# ``--log-path`` for tests / ad-hoc runs.
DEFAULT_LOG_PATH = pathlib.Path("/state/worker/thinking.log")


# ---------------------------------------------------------------------------
# argparse


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI's argument parser.

    Exposed as a separate function so tests can introspect the parser
    without spawning a subprocess.
    """
    parser = argparse.ArgumentParser(
        prog="alice-experiment",
        description=(
            "Dispatch an experiment subagent and wait for the result. "
            "Thin CLI shell over alice_thinking.experiments.ExperimentRunner "
            "for pi-mono callers that can't reach the run_experiment MCP "
            "tool. Prints a one-line JSON summary on stdout."
        ),
    )
    parser.add_argument(
        "--hypothesis",
        required=True,
        help="1-2 sentences naming what is being tested. Required.",
    )
    parser.add_argument(
        "--expected-output",
        required=True,
        help=(
            "What shape of output you want — 'metrics-table', 'summary-text', "
            "or 'file:<path>'. Required."
        ),
    )
    method_group = parser.add_mutually_exclusive_group(required=True)
    method_group.add_argument(
        "--inline-instructions",
        help=(
            "Literal prose describing the experiment's method. XOR with "
            "--method — pass exactly one."
        ),
    )
    method_group.add_argument(
        "--method",
        help=(
            "Path to a script (under /tmp/ or ~/alice-mind/) the subagent "
            "should execute / interpret. XOR with --inline-instructions."
        ),
    )
    parser.add_argument(
        "--tag",
        help=(
            "Optional slug tagged onto the event stream for grouping ad-hoc "
            "experiments. Currently surfaced in the 'experiment_cli_dispatch' "
            "event only; the card schema doesn't carry it."
        ),
    )
    parser.add_argument(
        "--context-paths",
        help=(
            "Comma-separated list of must-read-first file paths. Semantic "
            "hint, not access control. Each path must exist on disk."
        ),
    )
    parser.add_argument(
        "--repo-under-test",
        help=(
            "Optional. If set (today only 'alice' is supported), the runner "
            "rsyncs /home/alice/alice/ into /tmp/alice-copy-<id>/ and the "
            "subagent gets rw on that copy."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        help=(
            "Optional wall-clock timeout (default 1800 = 30 min). On timeout "
            "the subagent is killed, a failed-stub card is written, and the "
            "CLI exits 4."
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "Override the subagent model. Default is whatever the "
            "ExperimentRunner constructs with."
        ),
    )
    parser.add_argument(
        "--log-path",
        type=pathlib.Path,
        default=DEFAULT_LOG_PATH,
        help=(
            "Where to append CLI/runner events. Default: "
            f"{DEFAULT_LOG_PATH} (matches the production thinking wake)."
        ),
    )
    # --json defaults true. Use --no-json to also print human preamble.
    parser.add_argument(
        "--json",
        dest="json_only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Print only the JSON summary (default). Use --no-json to also "
            "print a two-line human-readable preamble before the JSON."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Input validation
#
# argparse handles required-ness and XOR via add_mutually_exclusive_group,
# but we still need post-parse checks for non-empty strings and
# path-existence on context_paths.


def _validate(args: argparse.Namespace) -> Optional[str]:
    """Return an error message describing the first invalid argument, or None.

    argparse already enforces required-ness and the method XOR; this
    catches the remaining holes:

    - whitespace-only ``--hypothesis`` / ``--expected-output``
    - ``--method`` pointing at a path that doesn't exist or isn't a file
      (the runner re-checks, but catching here gives a clean exit-2 with
      a usage message instead of an exit-3 dispatch error)
    - any ``--context-paths`` entry that doesn't exist on disk
    """
    if not args.hypothesis.strip():
        return "--hypothesis must be non-empty"
    if not args.expected_output.strip():
        return "--expected-output must be non-empty"
    if args.method is not None:
        method_path = pathlib.Path(args.method)
        if not method_path.is_file():
            return f"--method path not found or not a file: {args.method}"
    if args.inline_instructions is not None and not args.inline_instructions.strip():
        return "--inline-instructions must be non-empty"
    if args.context_paths:
        for raw in _split_context_paths(args.context_paths):
            p = pathlib.Path(raw)
            if not p.exists():
                return f"--context-paths entry does not exist: {raw}"
    return None


def _split_context_paths(raw: str) -> list[str]:
    """Split the comma-separated ``--context-paths`` value into clean strings."""
    return [part.strip() for part in raw.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Runner construction + auth fallback
#
# Same env-var precedence the MCP tool's wake-side construction uses
# (see alice_thinking.wake around line 650). Subagent-scoped key wins;
# fall through to ANTHROPIC_API_KEY for ad-hoc / non-production runs.


def _resolve_api_key() -> Optional[str]:
    return (
        os.environ.get("ANTHROPIC_API_KEY_SUBAGENT")
        or os.environ.get("ANTHROPIC_API_KEY")
        or None
    )


def _resolve_api_base_url() -> Optional[str]:
    return (
        os.environ.get("ANTHROPIC_BASE_URL_SUBAGENT")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or None
    )


def _build_runner(
    emitter: EventEmitter,
    *,
    runner_factory: Optional[Any] = None,
) -> ExperimentRunner:
    """Build the runner the CLI dispatches through.

    ``runner_factory`` is a test hook: tests pass a callable
    ``(emitter, api_key, api_base_url) -> ExperimentRunner`` so they can
    inject a runner with a mocked subprocess_runner without having to
    reach into the CLI's internals.
    """
    api_key = _resolve_api_key()
    api_base_url = _resolve_api_base_url()
    if runner_factory is not None:
        return runner_factory(
            emitter=emitter,
            api_key=api_key,
            api_base_url=api_base_url,
        )
    return ExperimentRunner(
        emitter=emitter,
        api_key=api_key,
        api_base_url=api_base_url,
    )


# ---------------------------------------------------------------------------
# Async core


async def _dispatch_and_wait(
    runner: ExperimentRunner,
    *,
    args: argparse.Namespace,
) -> tuple[int, dict[str, Any]]:
    """Dispatch one experiment and wait for the outcome.

    Returns ``(exit_code, payload)``. The payload is the dict the CLI
    prints to stdout regardless of exit code — even a timeout / failure
    surfaces a usable JSON object so the wake's Bash invocation has
    something to log.
    """
    context_paths = (
        _split_context_paths(args.context_paths) if args.context_paths else None
    )
    try:
        meta = runner.dispatch(
            hypothesis=args.hypothesis,
            method=args.method,
            inline_instructions=args.inline_instructions,
            expected_output=args.expected_output,
            context_paths=context_paths,
            repo_under_test=args.repo_under_test,
            timeout_seconds=args.timeout_seconds,
            model=args.model,
        )
    except ExperimentDispatchError as exc:
        return EXIT_DISPATCH_ERROR, {
            "experiment_id": None,
            "card_path": None,
            "status": "dispatch-error",
            "summary": "",
            "hypothesis": args.hypothesis,
            "error": str(exc),
        }

    # Emit a CLI-flavoured dispatch breadcrumb so the event stream can
    # distinguish CLI calls from MCP calls. The runner already emits
    # ``experiment_dispatch``; we layer one more event on top tagged
    # with the optional ``--tag`` slug for grouping.
    try:
        runner.emitter.emit(
            "experiment_cli_dispatch",
            experiment_id=meta.experiment_id,
            tag=args.tag,
            via="alice-experiment",
        )
    except Exception:  # noqa: BLE001
        # Observability never breaks the main path.
        pass

    try:
        outcome = await runner.wait_for(
            meta.experiment_id,
            timeout=args.timeout_seconds,
        )
    except UnknownExperimentError as exc:
        # Should not happen — dispatch just succeeded. Treat as dispatch
        # error so the operator gets a non-zero exit.
        return EXIT_DISPATCH_ERROR, {
            "experiment_id": meta.experiment_id,
            "card_path": str(meta.card_path),
            "status": "dispatch-error",
            "summary": "",
            "hypothesis": args.hypothesis,
            "error": str(exc),
        }
    except asyncio.TimeoutError:
        # Note: the runner has its own per-dispatch timeout (default
        # 30 min) that produces a failed-stub card. The CLI's
        # asyncio.wait_for is a belt-and-braces shim — same timeout
        # value, so in practice the runner's timeout fires first and
        # we hit the success path with a status=failed card. This
        # branch handles the corner case where wait_for itself is
        # cancelled (e.g. caller SIGINT mid-wait).
        return EXIT_TIMEOUT, {
            "experiment_id": meta.experiment_id,
            "card_path": str(meta.card_path),
            "status": "timeout",
            "summary": (
                "CLI wait_for timed out before the runner task drained. "
                "The background task continues; a failed-stub card may "
                "still land at card_path."
            ),
            "hypothesis": args.hypothesis,
            "error": f"timed out after {args.timeout_seconds}s",
        }

    payload = outcome.to_json_payload()
    exit_code = _exit_code_for_outcome(outcome)
    return exit_code, payload


def _exit_code_for_outcome(outcome: ExperimentOutcome) -> int:
    """Map an outcome's status to the CLI's exit code.

    ``complete`` and ``incomplete`` both exit 0 — ``incomplete`` is the
    subagent's self-report of "I finished but my conclusion is partial,"
    which is still a real result thinking can reason about. Hard failures
    (``failed``, ``missing-card``) exit 5.
    """
    if outcome.status in ("complete", "incomplete"):
        return EXIT_OK
    return EXIT_SUBAGENT_FAILURE


# ---------------------------------------------------------------------------
# Output


def _render_payload(payload: dict[str, Any], *, json_only: bool) -> str:
    """Render the final stdout text.

    The JSON is always a single line (no trailing newline from
    ``json.dumps``; we add the line break in :func:`run`). With
    ``--no-json`` the human preamble runs first; with ``--json`` (default)
    only the JSON line is emitted.
    """
    json_line = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    if json_only:
        return json_line
    preamble_lines = [
        f"alice-experiment {payload.get('experiment_id', '?')} -> {payload.get('status', '?')}",
        f"card: {payload.get('card_path', '?')}",
    ]
    return "\n".join(preamble_lines + [json_line])


# ---------------------------------------------------------------------------
# Sync entry points


def run(
    argv: Optional[list[str]] = None,
    *,
    emitter: Optional[EventEmitter] = None,
    runner_factory: Optional[Any] = None,
    stdout: Optional[Any] = None,
    stderr: Optional[Any] = None,
) -> int:
    """Run one CLI invocation. Returns the exit code.

    Split from :func:`main` so tests can drive the CLI with synthetic
    ``argv`` and inject an in-memory emitter / mocked runner factory
    without monkey-patching ``sys.argv`` or capturing the file descriptors
    the parser uses for ``--help``.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage to stderr. Preserve its exit
        # code for missing-required / bad-flag — but normalise to
        # EXIT_VALIDATION_ERROR for the conventional "2".
        rc = exc.code if isinstance(exc.code, int) else EXIT_VALIDATION_ERROR
        return rc if rc != 0 else EXIT_VALIDATION_ERROR

    validation_error = _validate(args)
    if validation_error is not None:
        parser.print_usage(file=err)
        print(f"alice-experiment: error: {validation_error}", file=err)
        return EXIT_VALIDATION_ERROR

    # Default emitter: file logger to the configured path. Tests inject
    # a CapturingEmitter so the assertions don't hit disk.
    if emitter is None:
        emitter = EventLogger(args.log_path)

    runner = _build_runner(emitter, runner_factory=runner_factory)

    try:
        exit_code, payload = asyncio.run(
            _dispatch_and_wait(runner, args=args)
        )
    except KeyboardInterrupt:
        print("alice-experiment: interrupted", file=err)
        return EXIT_TIMEOUT
    except Exception as exc:  # noqa: BLE001
        # Any unexpected error becomes a dispatch error — the CLI must
        # not crash silently. The traceback goes to stderr; stdout still
        # gets a parseable JSON line so the caller's logging is intact.
        import traceback

        traceback.print_exc(file=err)
        payload = {
            "experiment_id": None,
            "card_path": None,
            "status": "dispatch-error",
            "summary": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = EXIT_DISPATCH_ERROR

    print(_render_payload(payload, json_only=args.json_only), file=out)
    return exit_code


def main(argv: Optional[list[str]] = None) -> int:
    """Console-script entry point. Wraps :func:`run` for ``[project.scripts]``."""
    return run(argv)


if __name__ == "__main__":  # pragma: no cover - exercised via console_script
    sys.exit(main())
