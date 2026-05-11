"""``submit_result`` MCP server — exposed only inside the subagent.

The subagent (a subprocessed ``claude`` CLI) needs exactly one MCP tool:
``submit_result``. It writes the research-paper-shaped card to the vault
and signals successful completion to the parent runner.

Two ways the subagent talks to this server:

1. **In-process (test mode).** The parent runner builds the tool via
   :func:`build_submit_result_tool` and exposes it through an SDK MCP
   server. Used by ``alice_thinking.kernel_adapter`` when the runner is
   driven inside an asyncio task with the SDK (rare — only used for
   tests that don't want to subprocess the CLI).

2. **Stdio (production).** The runner spawns this module as a
   subprocess via ``python -m alice_thinking.experiments.submit_result
   --experiment-id <id> --output <path>`` and references it from the
   subagent's ``--mcp-config`` JSON as an stdio MCP server. The
   subagent invokes ``submit_result`` like any MCP tool; we write the
   card to ``<output>`` and exit.

Both paths write the same card via :func:`alice_thinking.experiments.card.write_card`.
The runner discovers the card on disk (or, in test mode, watches an
in-memory handoff event) and emits the completion side effects
(surface note, viewer event, jsonl line).

The CLI entry (path 2) writes a sidecar JSON status file alongside the
card so the runner can detect "subagent called submit_result vs.
subagent crashed before doing so." The status file's presence is the
signal — its content carries the timestamp and any extras.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import pathlib
import sys
from typing import Any, Callable, Optional

from .card import CardContent, write_card


__all__ = [
    "STATUS_FILE_NAME",
    "build_submit_result_tool",
    "main",
    "write_status_file",
]


log = logging.getLogger(__name__)


# Sidecar file the runner polls to detect "subagent successfully called
# submit_result." Lives next to the card file in the same directory the
# CLI is invoked with ``--status-dir``.
STATUS_FILE_NAME = "submit_result.status.json"


_SUBMIT_RESULT_DESCRIPTION = (
    "Write the experiment's research-paper-shaped result card. This is the "
    "subagent's ONLY way to surface a finding — call it once with the full "
    "set of sections filled in. The runner picks up the card and notifies "
    "the parent process. After calling, end the subagent."
)


_SUBMIT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Short H1 title for the experiment card.",
        },
        "abstract": {
            "type": "string",
            "description": (
                "2-3 sentence tl;dr — what was tested and what was found. "
                "Goes into the card frontmatter abstract and the surface note "
                "the parent sees on her next wake."
            ),
        },
        "hypothesis": {
            "type": "string",
            "description": (
                "Full hypothesis text with prior-art links. The first line is "
                "echoed into the card's frontmatter hypothesis field."
            ),
        },
        "method": {
            "type": "string",
            "description": (
                "Reproducibility-grade method section. Inline code/scripts or "
                "references to files. 6-months-later reader should be able "
                "to re-run from this."
            ),
        },
        "results": {
            "type": "string",
            "description": "Tables, numbers, plot references.",
        },
        "discussion": {
            "type": "string",
            "description": "What was learned, what surprised us, what's confounded.",
        },
        "conclusion": {
            "type": "string",
            "description": (
                "Bottom-line answer to the hypothesis: falsified, confirmed, "
                "or inconclusive."
            ),
        },
        "result_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional list of /tmp output paths (plots, metrics dumps) "
                "the runner should reference from the card frontmatter."
            ),
        },
        "cross_references": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of wikilinks for the Cross-references section.",
        },
        "status": {
            "type": "string",
            "enum": ["complete", "incomplete"],
            "description": (
                "Self-reported status. Default 'complete'; pick 'incomplete' "
                "when the conclusion is partial."
            ),
        },
    },
    "required": [
        "title",
        "abstract",
        "hypothesis",
        "method",
        "results",
        "discussion",
        "conclusion",
    ],
}


def _content_from_args(args: dict[str, Any]) -> CardContent:
    """Materialize a :class:`CardContent` from the MCP tool input."""
    return CardContent(
        title=str(args.get("title", "")),
        abstract=str(args.get("abstract", "")),
        hypothesis=str(args.get("hypothesis", "")),
        method=str(args.get("method", "")),
        results=str(args.get("results", "")),
        discussion=str(args.get("discussion", "")),
        conclusion=str(args.get("conclusion", "")),
        result_paths=list(args.get("result_paths") or []),
        cross_references=list(args.get("cross_references") or []),
    )


def write_status_file(
    status_dir: pathlib.Path,
    *,
    experiment_id: str,
    card_path: pathlib.Path,
    status: str,
    completed_at: Optional[datetime.datetime] = None,
) -> pathlib.Path:
    """Drop the sidecar status file the runner polls.

    Presence of this file == "subagent called submit_result successfully."
    Absence == "no submit_result was called → write failed-stub card."

    Returns the path written.
    """
    if completed_at is None:
        completed_at = datetime.datetime.now().astimezone()
    target = status_dir / STATUS_FILE_NAME
    payload = {
        "experiment_id": experiment_id,
        "card_path": str(card_path),
        "status": status,
        "completed_at": completed_at.replace(microsecond=0).isoformat(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))
    return target


def build_submit_result_tool(
    *,
    experiment_id: str,
    card_path: pathlib.Path,
    dispatched_at: datetime.datetime,
    transcript_path: pathlib.Path,
    repo_under_test: Optional[str] = None,
    status_dir: Optional[pathlib.Path] = None,
    on_submitted: Optional[Callable[[CardContent, str], None]] = None,
):
    """Build the in-process ``submit_result`` SdkMcpTool.

    Used by the in-process / test path (no subprocess). The runner's CLI
    spawner uses :func:`main` for the production stdio path.

    The tool writes the card directly to ``card_path``, drops the status
    file into ``status_dir`` (default: ``card_path.parent``), and invokes
    ``on_submitted(content, status)`` if provided so the parent can break
    out of any waiting loop.
    """
    from claude_agent_sdk import tool

    target_status_dir = status_dir or card_path.parent

    @tool(
        name="submit_result",
        description=_SUBMIT_RESULT_DESCRIPTION,
        input_schema=_SUBMIT_RESULT_SCHEMA,
    )
    async def submit_result(args: dict) -> dict:
        content = _content_from_args(args)
        status_value = str(args.get("status") or "complete")
        if status_value not in ("complete", "incomplete"):
            status_value = "complete"
        completed_at = datetime.datetime.now().astimezone()
        try:
            write_card(
                card_path,
                experiment_id=experiment_id,
                content=content,
                dispatched_at=dispatched_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - dispatched_at).total_seconds(),
                transcript_path=str(transcript_path),
                repo_under_test=repo_under_test,
                status=status_value,
            )
            write_status_file(
                target_status_dir,
                experiment_id=experiment_id,
                card_path=card_path,
                status=status_value,
                completed_at=completed_at,
            )
        except OSError as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"error: failed to write card: {type(exc).__name__}: {exc}",
                    }
                ],
                "isError": True,
            }
        if on_submitted is not None:
            try:
                on_submitted(content, status_value)
            except Exception:  # noqa: BLE001
                log.exception("on_submitted callback failed for %s", experiment_id)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Card written to {card_path} (status={status_value}). "
                        "End your turn — the runner takes it from here."
                    ),
                }
            ]
        }

    return submit_result


# ---------------------------------------------------------------------------
# Stdio MCP server entry — invoked as `python -m alice_thinking.experiments.submit_result`
#
# The subagent's ``--mcp-config`` points at this. We expose exactly one tool,
# ``submit_result``, which writes the card + status file to disk and then
# returns. The runner polls for the status file.


def _build_server(args: argparse.Namespace):
    """Construct the SDK MCP server for the stdio entry point."""
    from claude_agent_sdk import create_sdk_mcp_server

    dispatched_at = datetime.datetime.fromisoformat(args.dispatched_at)
    card_path = pathlib.Path(args.card_path)
    status_dir = pathlib.Path(args.status_dir)
    transcript_path = pathlib.Path(args.transcript_path)
    tool_obj = build_submit_result_tool(
        experiment_id=args.experiment_id,
        card_path=card_path,
        dispatched_at=dispatched_at,
        transcript_path=transcript_path,
        repo_under_test=args.repo_under_test or None,
        status_dir=status_dir,
    )
    return create_sdk_mcp_server(
        name="alice_experiment",
        version="0.1.0",
        tools=[tool_obj],
    )


async def _run_stdio(args: argparse.Namespace) -> int:
    """Drive the stdio MCP server until the parent CLI exits.

    SDK servers built via ``create_sdk_mcp_server`` expose a
    ``run_stdio()`` coroutine; if the SDK version doesn't, fall back to
    the ``run`` method. Any error is logged and surfaces as a non-zero
    exit code so the runner can detect a misconfigured invocation.
    """
    server = _build_server(args)
    runner = getattr(server, "run_stdio", None) or getattr(server, "run", None)
    if runner is None:
        log.error("MCP server has no run_stdio/run method; SDK incompatible")
        return 2
    try:
        result = runner()
        if asyncio.iscoroutine(result):
            await result
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001
        log.exception("stdio MCP server failed")
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry — invoked as ``python -m alice_thinking.experiments.submit_result``."""
    parser = argparse.ArgumentParser(description="submit_result MCP server")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--card-path", required=True)
    parser.add_argument("--status-dir", required=True)
    parser.add_argument("--transcript-path", required=True)
    parser.add_argument("--dispatched-at", required=True, help="ISO-8601 timestamp")
    parser.add_argument("--repo-under-test", default="")
    parsed = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return asyncio.run(_run_stdio(parsed))


if __name__ == "__main__":
    sys.exit(main())
