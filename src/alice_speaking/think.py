"""Thinking Alice — one wake, driven through the Claude Agent SDK.

Direct `claude -p ...` invocations hang on TTY detection in non-interactive
environments (Issue #9026). The Agent SDK avoids this by using the
stream-json input protocol. Since we already depend on the SDK for speaking
Alice, reusing it here gives us:

- Deterministic execution in cron/docker (no TTY needed)
- Structured per-event logging (tool_use, tool_result, assistant_text, result)
- Simple --quick mode for sub-30s plumbing tests
- Same venv, same auth path, same observability

Log format: one JSON event per line to /state/worker/thinking.log, with wall
clock timestamps. Compatible with `tail -f | jq -c .` for live inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_BOOTSTRAP = DEFAULT_MIND / "prompts" / "thinking-bootstrap.md"
DEFAULT_LOG = pathlib.Path("/state/worker/thinking.log")
DEFAULT_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_SECONDS = 0  # 0 == no timeout. Thinking runs as long as it needs.
QUICK_PROMPT = "Reply exactly: QUICK-OK"
QUICK_MAX_SECONDS = 30


def _load_token() -> None:
    """Populate ``CLAUDE_CODE_OAUTH_TOKEN`` in os.environ (no-op if already set).

    Delegates to :func:`alice_core.auth.ensure_token` — see that module for
    the resolution order.
    """
    from alice_core.auth import ensure_token

    ensure_token()


def _apply_config_overrides(args: argparse.Namespace) -> None:
    """Pull thinking.* overrides out of alice.config.json if they exist."""
    cfg_path = pathlib.Path(args.mind) / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError:
        return
    think = (cfg or {}).get("thinking") or {}
    if args.model == DEFAULT_MODEL and "model" in think:
        args.model = think["model"]
    if args.max_seconds == DEFAULT_MAX_SECONDS and "max_wake_seconds" in think:
        args.max_seconds = int(think["max_wake_seconds"])
    if args.tools == DEFAULT_TOOLS and "allowed_tools" in think:
        args.tools = ",".join(think["allowed_tools"])


class EventLogger:
    def __init__(self, log_path: pathlib.Path, echo: bool = False) -> None:
        self.log_path = log_path
        self.echo = echo
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self.log_path.open("a") as f:
            f.write(line + "\n")
        if self.echo:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()


def _short(obj: Any, cap: int = 400) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[: cap - 1] + "…"


async def _run_once(
    *,
    prompt_text: str,
    model: str,
    tools: list[str],
    cwd: pathlib.Path,
    max_seconds: int,
    logger: EventLogger,
) -> int:
    options = ClaudeAgentOptions(
        model=model,
        allowed_tools=tools,
        cwd=str(cwd),
    )
    logger.emit(
        "wake_start",
        model=model,
        max_seconds=max_seconds,
        tools=tools,
        cwd=str(cwd),
        prompt_chars=len(prompt_text),
    )

    async def _drive() -> None:
        async for msg in query(prompt=prompt_text, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        logger.emit("assistant_text", text=_short(block.text))
                    elif isinstance(block, ToolUseBlock):
                        logger.emit(
                            "tool_use",
                            name=block.name,
                            input=_short(block.input),
                            id=block.id,
                        )
                    elif isinstance(block, ThinkingBlock):
                        logger.emit("thinking", text=_short(block.thinking))
                if msg.error:
                    logger.emit("assistant_error", error=msg.error)
            elif isinstance(msg, UserMessage):
                # UserMessage comes back when tool results arrive; log for
                # completeness so the trace is faithful.
                logger.emit("user_message", content=_short(msg.content))
            elif isinstance(msg, ResultMessage):
                logger.emit(
                    "result",
                    num_turns=msg.num_turns,
                    duration_ms=msg.duration_ms,
                    total_cost_usd=msg.total_cost_usd,
                    is_error=msg.is_error,
                    usage=msg.usage,
                    result=_short(msg.result) if msg.result else None,
                )
            elif isinstance(msg, SystemMessage):
                logger.emit(
                    "system",
                    subtype=msg.subtype,
                    data_keys=list((msg.data or {}).keys()),
                )

    try:
        if max_seconds and max_seconds > 0:
            async with asyncio.timeout(max_seconds):
                await _drive()
        else:
            # max_seconds <= 0 → unbounded. Thinking finishes on its own terms.
            await _drive()
    except asyncio.TimeoutError:
        logger.emit("timeout", max_seconds=max_seconds)
        return 124
    except Exception as exc:  # noqa: BLE001
        logger.emit("exception", type=type(exc).__name__, message=str(exc))
        return 1
    logger.emit("wake_end")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One thinking wake (Claude Agent SDK)."
    )
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument("--bootstrap", default=None, help="prompt file (default: mind/prompts/thinking-bootstrap.md)")
    parser.add_argument("--prompt", default=None, help="inline prompt (overrides --bootstrap)")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="event log path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_MAX_SECONDS,
        help="Wake budget in seconds. 0 or negative = no timeout (default).",
    )
    parser.add_argument("--tools", default=DEFAULT_TOOLS)
    parser.add_argument("--echo", action="store_true", help="also echo events to stderr")
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "30s plumbing smoke test — tiny prompt, no tools, cwd=/tmp. "
            "Verifies SDK + OAuth + Sonnet end-to-end without running the real "
            "thinking workflow."
        ),
    )
    args = parser.parse_args()

    _load_token()
    _apply_config_overrides(args)

    logger = EventLogger(pathlib.Path(args.log), echo=args.echo)

    if args.quick:
        prompt_text = QUICK_PROMPT
        tools: list[str] = []
        cwd = pathlib.Path("/tmp")
        max_seconds = QUICK_MAX_SECONDS
    else:
        mind = pathlib.Path(args.mind)
        prompt_text = (
            args.prompt
            if args.prompt
            else pathlib.Path(args.bootstrap or (mind / "prompts" / "thinking-bootstrap.md")).read_text()
        )
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
        cwd = mind
        max_seconds = args.max_seconds

    return asyncio.run(
        _run_once(
            prompt_text=prompt_text,
            model=args.model,
            tools=tools,
            cwd=cwd,
            max_seconds=max_seconds,
            logger=logger,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
