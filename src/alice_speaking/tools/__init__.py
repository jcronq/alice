"""Custom SDK tools for speaking Alice.

Exposes dedicated affordances over the built-in Bash/Read/Write tools so Alice
has semantic operations for tending her inner life: the directive, notes,
thoughts, memory, and her own runtime config. These are in-process MCP tools
registered with the Claude Agent SDK via create_sdk_mcp_server.

Tools are built via a factory (`build`) so each one closes over the Config —
no module-level state.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server

from ..config import Config
from . import config_tools, inner, memory


SERVER_NAME = "alice"


def build(cfg: Config) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Return the mcp_servers dict and the fully-qualified allowed_tools list
    for ClaudeAgentOptions."""
    tools: list[Any] = [
        *inner.build(cfg),
        *memory.build(cfg),
        *config_tools.build(cfg),
    ]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    # Agent SDK scopes MCP tools as `mcp__<server>__<tool>` in allowed_tools.
    allowed = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return {SERVER_NAME: server}, allowed
