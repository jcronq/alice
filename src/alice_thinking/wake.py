"""Thinking Alice — one wake, driven through the agent kernel.

This is the one-shot entry point invoked by ``/usr/local/bin/alice-think``
from the cron-style s6 supervisor. Each invocation:

1. Loads the OAuth token into the environment (via ``alice_core.auth``).
2. Applies ``thinking.*`` overrides from ``alice.config.json``.
3. Reads the bootstrap prompt from ``alice-mind/prompts/thinking-bootstrap.md``
   (or ``--prompt`` for inline prompts; ``--quick`` for a plumbing test).
4. Instantiates :class:`alice_core.kernel.AgentKernel` with a JSONL
   :class:`EventLogger` pointed at ``/state/worker/thinking.log``.
5. Calls ``kernel.run(prompt, spec)`` and returns.

No handlers are composed — thinking doesn't persist sessions across
wakes (each is fresh) and doesn't compact (Sonnet stays small by the
"one small pass per wake" ethos). The SDK's structured events flow
straight to the log for the alice-viewer to tail.

Moves in step 8 to its own ``alice_thinking`` package; for now still
lives in ``alice_speaking`` alongside the daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from alice_core.auth import ensure_auth_env
from alice_core.events import EventLogger
from alice_core.kernel import AgentKernel, KernelSpec


WAKE_TZ = ZoneInfo("America/New_York")


DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_BOOTSTRAP = DEFAULT_MIND / "prompts" / "thinking-bootstrap.md"
DEFAULT_DIRECTIVE = DEFAULT_MIND / "inner" / "directive.md"
DEFAULT_LOG = pathlib.Path("/state/worker/thinking.log")
DEFAULT_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_SECONDS = 0  # 0 == no timeout. Thinking runs as long as it needs.
QUICK_MAX_SECONDS = 30
# QUICK_PROMPT moved to alice_prompts/templates/thinking/quick.md.j2
# (Plan 04 Phase 1). Use ``alice_prompts.load("thinking.quick")``.


def _wake_timestamp_header(now: datetime | None = None) -> str:
    """Return a single-line wake-time header for the prompt.

    Injected at the very top of Thinking's prompt so she never has to
    compute the local hour herself — that path was brittle (the bootstrap
    instruction can drift out of sync with mode/stage logic, as it did
    during the sleep-arch v2 rollout). Format:
    ``Current local time: 2026-04-26 14:32 EDT (Sunday)``.
    DST is handled by zoneinfo; we don't hardcode the abbreviation.
    """
    moment = (now or datetime.now(WAKE_TZ)).astimezone(WAKE_TZ)
    return (
        "Current local time: "
        f"{moment.strftime('%Y-%m-%d %H:%M %Z')} ({moment.strftime('%A')})"
    )


def _build_prompt(bootstrap_path: pathlib.Path, directive_path: pathlib.Path) -> str:
    """Compose the wake prompt: timestamp + directive contents + bootstrap.

    Inlining directive.md saves the agent one Read tool round-trip per
    wake. The directive is small (~30 lines, well under the 1024-token
    cache threshold) — the win is the saved round-trip, not caching.

    The wake timestamp is prepended so Thinking sees current local time
    before any other prompt content. Mode/stage selection should read
    this line rather than computing the hour themselves.
    """
    bootstrap = bootstrap_path.read_text()
    header = _wake_timestamp_header()
    if not directive_path.is_file():
        return f"{header}\n\n{bootstrap}"
    directive = directive_path.read_text()
    return (
        f"{header}\n\n"
        "## Directive (current standing orders — read this first)\n\n"
        f"{directive.strip()}\n\n"
        "---\n\n"
        f"{bootstrap}"
    )


def _load_token() -> None:
    """Resolve auth from alice.env + os.environ into the subprocess env.

    Thin wrapper over :func:`alice_core.auth.ensure_auth_env` — kept
    as a local function so the module's public API stays stable.
    Sets either ``CLAUDE_CODE_OAUTH_TOKEN`` (subscription mode) or
    ``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_API_KEY`` (api / LiteLLM mode)
    so the Agent SDK's CLI subprocess inherits the right credentials.
    """
    ensure_auth_env()


def _apply_config_overrides(args: argparse.Namespace) -> None:
    """Pull thinking.* overrides out of alice.config.json if they exist.

    Only overrides values the user didn't explicitly pass on the CLI:
    CLI args > config file > module defaults.
    """
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


async def _run_wake(
    *,
    prompt_text: str,
    model: str,
    tools: list[str],
    cwd: pathlib.Path,
    max_seconds: int,
    emitter: EventLogger,
) -> int:
    """One thinking wake through the agent kernel.

    Emits a ``wake_start`` envelope event around the kernel.run() call, then
    ``wake_end`` on clean finish (or lets the kernel's ``timeout`` / propagated
    exception carry the error signal). Returns a process-friendly exit code:
    0 on clean, 124 on timeout (matches the GNU timeout convention), 1 otherwise.
    """
    wake_id = f"wake-{int(time.time())}"
    emitter.emit(
        "wake_start",
        wake_id=wake_id,
        model=model,
        max_seconds=max_seconds,
        tools=tools,
        cwd=str(cwd),
        prompt_chars=len(prompt_text),
    )

    kernel = AgentKernel(
        emitter,
        correlation_id=wake_id,
        # Cap is generous — Sonnet's reasoning blocks are often >1k chars
        # and a wake's whole value is the trace (the owner browses thoughts
        # in the viewer, not just the resulting wiki edits).
        short_cap=4000,
    )
    spec = KernelSpec(
        model=model,
        allowed_tools=tools,
        cwd=cwd,
        max_seconds=max_seconds,
        # Adaptive thinking with summarized display so ThinkingBlocks
        # come back with non-empty text.
        thinking={"type": "adaptive", "display": "summarized"},
    )

    try:
        result = await kernel.run(prompt_text, spec)
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "exception",
            wake_id=wake_id,
            type=type(exc).__name__,
            message=str(exc),
        )
        return 1

    if result.error == "timeout":
        # Kernel already emitted the ``timeout`` event; surface exit code.
        return 124

    emitter.emit("wake_end", wake_id=wake_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One thinking wake (Claude Agent kernel)."
    )
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument(
        "--bootstrap",
        default=None,
        help="prompt file (default: mind/prompts/thinking-bootstrap.md)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="inline prompt (overrides --bootstrap)",
    )
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="event log path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_MAX_SECONDS,
        help="Wake budget in seconds. 0 or negative = no timeout (default).",
    )
    parser.add_argument("--tools", default=DEFAULT_TOOLS)
    parser.add_argument(
        "--echo", action="store_true", help="also echo events to stderr"
    )
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

    emitter = EventLogger(pathlib.Path(args.log), echo=args.echo)

    if args.quick:
        from alice_prompts import load as load_prompt
        prompt_text = load_prompt("thinking.quick")
        tools: list[str] = []
        cwd = pathlib.Path("/tmp")
        max_seconds = QUICK_MAX_SECONDS
    else:
        mind = pathlib.Path(args.mind)
        if args.prompt:
            prompt_text = args.prompt
        else:
            bootstrap_path = pathlib.Path(
                args.bootstrap or (mind / "prompts" / "thinking-bootstrap.md")
            )
            prompt_text = _build_prompt(bootstrap_path, mind / "inner" / "directive.md")
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
        cwd = mind
        max_seconds = args.max_seconds

    return asyncio.run(
        _run_wake(
            prompt_text=prompt_text,
            model=args.model,
            tools=tools,
            cwd=cwd,
            max_seconds=max_seconds,
            emitter=emitter,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
