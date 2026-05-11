"""``run_experiment`` MCP tool — thinking-side async experiment dispatch.

Lives on the thinking hemisphere's MCP server (not speaking — speaking has
no business dispatching experiments; she has Signal latency budgets to
mind). Implements the v2 spec from
``inner/notes/2026-05-11-115827-design-proposal.md`` (async hand-off,
experiment cards as canonical results, UI canvas hook).

Tool signature::

    run_experiment(
        hypothesis: str,
        method: str | None,            # path to script in /tmp/ or ~/alice-mind/
        inline_instructions: str | None,  # XOR with method
        expected_output: str,          # "metrics-table" | "summary-text" | "file:<path>"
        context_paths: list[str],      # "must-read-first" hint, not access control
        repo_under_test: str | None,   # if set, /tmp/alice-copy-<id>/ is rw-mounted
    ) -> dict

Returns immediately with ``experiment_id``, ``status="dispatched"``,
``card_path``, ``transcript_path``, ``dispatched_at``.

The :class:`ExperimentRunner` does the actual subprocess work in an
asyncio task. Thinking's wake closes after this call; the side effects
(card write, surface note, viewer event, jsonl line) land asynchronously.
Thinking picks up the result on her next wake via the surface watcher.
"""

from __future__ import annotations

from typing import Any, Optional

from claude_agent_sdk import SdkMcpTool, tool

from alice_core.events import EventEmitter

from ..experiments import ExperimentDispatchError, ExperimentRunner


__all__ = [
    "build_run_experiment_tool",
]


_DESCRIPTION = (
    "Dispatch an async experiment to a sandboxed `claude` CLI subagent. "
    "Returns immediately with dispatch metadata — the subagent runs "
    "detached and writes a research-paper-shaped result card to "
    "`cortex-memory/experiments/<id>.md` when it finishes (or fails). "
    "Your wake closes after this call; you pick up the result on your "
    "next wake via the surface watcher. Use this for any "
    "evaluation-first work: retrieval evals, ranking experiments, "
    "code-patch benchmarks. Method (script path) and inline_instructions "
    "are XOR — pass exactly one. Set `repo_under_test` to mount a "
    "writable copy of the alice repo at /tmp/alice-copy-<id>/."
)


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis": {
            "type": "string",
            "description": (
                "1-2 sentences naming what is being tested. Goes into the "
                "card frontmatter + the surface note for next-wake pickup."
            ),
        },
        "method": {
            "type": "string",
            "description": (
                "Path to a script (under /tmp/ or ~/alice-mind/) the subagent "
                "should execute / interpret as the experiment's method. XOR "
                "with `inline_instructions` — pass exactly one of the two."
            ),
        },
        "inline_instructions": {
            "type": "string",
            "description": (
                "Literal prose describing the experiment's method. Use this "
                "when the experiment is small enough that a script would be "
                "ceremony. XOR with `method`."
            ),
        },
        "expected_output": {
            "type": "string",
            "description": (
                "What shape of output you want: 'metrics-table', "
                "'summary-text', or 'file:<path>'. Guides the subagent "
                "without forcing structure."
            ),
        },
        "context_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional list of must-read-first file paths. Semantic hint, "
                "NOT access control — the subagent can read anywhere in the "
                "sandbox; this just signals priority."
            ),
        },
        "repo_under_test": {
            "type": "string",
            "description": (
                "Optional. If set (today only `alice` is supported), the "
                "runner rsyncs /home/alice/alice/ into "
                "/tmp/alice-copy-<id>/ and the subagent gets rw on that copy. "
                "Real repo stays read-only. Copy is GC'd after the card writes."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": (
                "Optional safety-net wall-clock timeout (default 1800 = 30 min). "
                "On timeout the subagent is killed and a failed-stub card is "
                "written. Not a kill-after expectation — most experiments "
                "finish in seconds; this catches livelocks."
            ),
        },
    },
    "required": ["hypothesis", "expected_output"],
}


def _ok(text: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Wrap a success payload in the MCP tool-result envelope.

    ``extra`` rides alongside the text content so structured callers can
    decode the metadata without re-parsing the text body. The text body
    is human-readable telemetry; the structured fields are for the
    parent process.
    """
    response: dict[str, Any] = {
        "content": [{"type": "text", "text": text}]
    }
    if extra:
        response.update(extra)
    return response


def _err(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"error: {text}"}],
        "isError": True,
    }


def build_run_experiment_tool(
    *,
    emitter: EventEmitter,
    runner: Optional[ExperimentRunner] = None,
    api_key: Optional[str] = None,
    api_base_url: Optional[str] = None,
) -> SdkMcpTool[Any]:
    """Build the ``run_experiment`` SdkMcpTool.

    ``runner`` is the shared :class:`ExperimentRunner` instance. When
    omitted, a default runner is constructed lazily on first call with
    the provided ``api_key`` / ``api_base_url``. Tests typically inject a
    pre-built runner with a mocked subprocess_runner.
    """
    # Lazy state — held in a closure list so we can re-bind without
    # ``nonlocal`` ceremony every call.
    _runner_holder: list[Optional[ExperimentRunner]] = [runner]

    def _get_runner() -> ExperimentRunner:
        if _runner_holder[0] is None:
            _runner_holder[0] = ExperimentRunner(
                emitter=emitter,
                api_key=api_key,
                api_base_url=api_base_url,
            )
        return _runner_holder[0]

    @tool(
        name="run_experiment",
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
    )
    async def run_experiment(args: dict) -> dict:
        hypothesis = args.get("hypothesis") or ""
        method = args.get("method")
        inline_instructions = args.get("inline_instructions")
        expected_output = args.get("expected_output") or ""
        context_paths = args.get("context_paths")
        repo_under_test = args.get("repo_under_test")
        timeout_seconds = args.get("timeout_seconds")

        # Normalize "" -> None so the runner's XOR check works cleanly.
        if isinstance(method, str) and not method.strip():
            method = None
        if isinstance(inline_instructions, str) and not inline_instructions.strip():
            inline_instructions = None
        if isinstance(repo_under_test, str) and not repo_under_test.strip():
            repo_under_test = None

        try:
            meta = _get_runner().dispatch(
                hypothesis=hypothesis,
                method=method,
                inline_instructions=inline_instructions,
                expected_output=expected_output,
                context_paths=context_paths,
                repo_under_test=repo_under_test,
                timeout_seconds=timeout_seconds,
            )
        except ExperimentDispatchError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(f"{type(exc).__name__}: {exc}")

        payload = meta.to_tool_response()
        summary = (
            f"Dispatched experiment {meta.experiment_id}. "
            f"Card will land at {meta.card_path}; transcript at "
            f"{meta.transcript_path}. End your wake — the runner takes it from here."
        )
        return _ok(summary, extra=payload)

    return run_experiment
