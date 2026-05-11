"""``dispatch_background_task`` MCP tool — fire-and-walk sub-agent.

Solves a real lock-time problem: when Alice triggers the SDK's
built-in ``Agent`` (Task) tool, the parent turn stays open for the
sub-agent's full duration (observed: 13-minute turn for an 11-minute
sub-agent, with the daemon lock held the whole time, queueing every
inbound Signal message until the parent returned).

This tool decouples the long work from the turn:

1. Caller passes ``description`` (short label for correlation later)
   and ``instructions`` (what the sub-agent should do).
2. Daemon spawns a fresh stock-Claude sub-agent in an asyncio task
   with no MCP servers and no Alice persona — pure built-in tools
   (Read/Write/Edit/Bash/Glob/Grep/WebFetch). It can't recursively
   dispatch more sub-agents and can't talk back to Signal directly.
3. The tool returns the dispatch handle immediately. The parent
   SDK call never blocks on the sub-agent.
4. Alice's parent turn ends naturally (typically a one-line wrap-up
   then ``stop_reason=end_turn``); the daemon lock releases.
5. When the sub-agent finishes, the daemon-owned waiter pushes a
   :class:`BackgroundTaskCompleteEvent` onto the dispatcher queue.
   That triggers a fresh turn for Alice with the result text, on
   the originating channel, so she can review and forward.

Optional ``user_facing_message`` lets Alice attach a short text to
the dispatch — sent via the normal outbox before the tool returns,
so the originating user sees acknowledgment immediately.

Optional ``tasks`` list supports fan-out (one tool call → N parallel
sub-agents). Per Alice's data, ~15% of turns dispatch multiple
agents in one turn; without fan-out the constraint would force
multiple turns just to kick off parallel work.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import SdkMcpTool, tool

from alice_core.config.personae import Personae

from ..infra.config import Config


log = logging.getLogger(__name__)


# Dispatcher closure provided by the daemon. Returns the freshly
# minted handle (``bg-<short-uuid>``) for the new sub-agent.
DispatchCallable = Callable[[str, str], Awaitable[str]]


# Outbound text-sender, optional — wraps the daemon's _send_message
# closure so the tool can deliver ``user_facing_message`` on the
# originating channel before the tool returns. The ``recipient``
# string here is interpreted by the underlying messaging tool; we
# pass "self" to route back over the inbound channel.
TextSenderCallable = Callable[[str], Awaitable[None]]


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "Short label (a few words) that captures what this "
                "sub-agent is doing — surfaced back in the completion "
                "event so you can map the result to whatever you "
                "promised the user."
            ),
        },
        "instructions": {
            "type": "string",
            "description": (
                "The full prompt for the sub-agent. It runs as a stock "
                "Claude with Bash/Read/Write/Edit/Glob/Grep/WebFetch — "
                "no MCP tools, no memory of yours, no Signal access. "
                "Write self-contained instructions; assume zero "
                "shared context."
            ),
        },
        "user_facing_message": {
            "type": "string",
            "description": (
                "Optional. A short text sent to the originating "
                "channel BEFORE this tool returns, so the user sees "
                "immediate acknowledgment. Skip it if your turn's "
                "next move is a separate send_message call."
            ),
        },
        "tasks": {
            "type": "array",
            "description": (
                "Optional. For parallel fan-out: a list of "
                "{description, instructions} entries that each spawn "
                "their own sub-agent. When present, the top-level "
                "``description`` and ``instructions`` are ignored. "
                "Returns a list of handles instead of a single id."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "instructions": {"type": "string"},
                },
                "required": ["description", "instructions"],
            },
        },
    },
    "required": [],
}


def build(
    cfg: Config,
    *,
    dispatcher: DispatchCallable,
    text_sender: Optional[TextSenderCallable] = None,
    personae: Optional[Personae] = None,
) -> list[SdkMcpTool[Any]]:
    """Build the dispatch_background_task tool list.

    ``dispatcher`` is the daemon-supplied closure that actually spawns
    the sub-agent and returns its handle; this module knows nothing
    about the daemon's task registry or kernel construction.

    ``text_sender`` is an optional one-arg sender for the
    ``user_facing_message`` flow. When None (e.g. tests, harnesses
    without an outbox), specifying ``user_facing_message`` is a no-op
    with a logged warning rather than an error — the dispatch itself
    is the load-bearing operation.
    """
    # personae is accepted for parity with sibling tool builders even
    # though this tool's description doesn't interpolate any names.
    del personae

    @tool(
        name="dispatch_background_task",
        description=(
            "Fire off a sub-agent in the background and return "
            "immediately. The parent turn does NOT wait for the "
            "sub-agent — your turn ends naturally; lock releases; "
            "users can talk to you again. When the sub-agent "
            "finishes, you'll get a fresh inbound turn with the "
            "result and can decide what to forward.\n\n"
            "Use this for any task that would otherwise tie up your "
            "turn for more than a few seconds (installs, multi-step "
            "research, batch processing). Don't use it for trivial "
            "work — the round-trip overhead isn't worth it. The "
            "sub-agent runs as stock Claude with built-in tools "
            "only; no access to your MCP, memory, or Signal."
        ),
        input_schema=_INPUT_SCHEMA,
    )
    async def dispatch_background_task(args: dict) -> dict:
        tasks = args.get("tasks") or []
        user_facing_message = args.get("user_facing_message")

        if tasks:
            handles: list[str] = []
            for entry in tasks:
                desc = (entry.get("description") or "").strip()
                instr = (entry.get("instructions") or "").strip()
                if not desc or not instr:
                    continue
                handle = await dispatcher(desc, instr)
                handles.append(handle)
            if not handles:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "No tasks dispatched — every entry was "
                                "missing description or instructions."
                            ),
                        }
                    ],
                    "isError": True,
                }
            await _maybe_send(text_sender, user_facing_message)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Dispatched {len(handles)} background "
                            f"task(s): {', '.join(handles)}. "
                            "You'll receive a separate completion "
                            "turn for each as they finish."
                        ),
                    }
                ],
                "handles": handles,
                "task_count": len(handles),
                "status": "dispatched",
            }

        description = (args.get("description") or "").strip()
        instructions = (args.get("instructions") or "").strip()
        if not description or not instructions:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "dispatch_background_task requires both "
                            "`description` and `instructions` (or a "
                            "non-empty `tasks` list for fan-out)."
                        ),
                    }
                ],
                "isError": True,
            }
        handle = await dispatcher(description, instructions)
        await _maybe_send(text_sender, user_facing_message)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Dispatched background task {handle} "
                        f"({description!r}). You'll receive a "
                        "completion turn when it finishes."
                    ),
                }
            ],
            "id": handle,
            "task_count": 1,
            "status": "dispatched",
        }

    return [dispatch_background_task]


async def _maybe_send(
    text_sender: Optional[TextSenderCallable], message: Optional[str]
) -> None:
    """Send the optional user-facing acknowledgment, swallowing any
    transport error so a failed wrap-up doesn't undo a successful
    dispatch (the sub-agent is already running)."""
    if not message or not text_sender:
        if message and not text_sender:
            log.warning(
                "user_facing_message specified but no text_sender wired; "
                "dropping the acknowledgment."
            )
        return
    try:
        await text_sender(message)
    except Exception:
        log.exception(
            "user_facing_message send failed; dispatch itself succeeded"
        )


__all__ = ["build", "DispatchCallable", "TextSenderCallable"]
