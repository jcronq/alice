"""Custom SDK tools for speaking Alice.

Exposes dedicated affordances over the built-in Bash/Read/Write tools so Alice
has semantic operations for tending her inner life: the directive, notes,
thoughts, memory, her own runtime config, and the explicit outbox
(send_message).

Tools are built via a factory (`build`) so each one closes over the Config —
no module-level state.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server

from ..config import Config
from ..signal_client import SignalClient
from . import config_tools, inner, memory, messaging


SERVER_NAME = "alice"


def build(
    cfg: Config,
    *,
    signal: Optional[SignalClient] = None,
    sender: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Return the mcp_servers dict and the fully-qualified allowed_tools list
    for ClaudeAgentOptions.

    ``signal`` or ``sender`` is optional — when both are omitted the
    send_message tool is skipped (useful for tests and the think-hemisphere
    harness). Daemon callers should pass their own ``sender`` closure so the
    daemon can track whether a turn produced any outbound (missed_reply
    detection).
    """
    tool_list: list[Any] = [
        *inner.build(cfg),
        *memory.build(cfg),
        *config_tools.build(cfg),
    ]
    if sender is not None or signal is not None:
        tool_list.extend(messaging.build(cfg, signal=signal, sender=sender))
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tool_list)
    # Agent SDK scopes MCP tools as `mcp__<server>__<tool>` in allowed_tools.
    allowed = [f"mcp__{SERVER_NAME}__{t.name}" for t in tool_list]
    return {SERVER_NAME: server}, allowed
