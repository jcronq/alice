"""Custom SDK tools for the speaking hemisphere.

Exposes dedicated affordances over the built-in Bash/Read/Write tools so the
agent has semantic operations for tending her inner life: the directive,
notes, thoughts, memory, her own runtime config, and the explicit outbox
(send_message).

Tools are built via a factory (`build`) so each one closes over the Config —
no module-level state. Plan 05 Phase 5 added a ``personae`` argument so
descriptions can substitute the configured agent + user names instead of
hardcoding ``"Alice"`` / ``"owner"``.
"""

from __future__ import annotations

from typing import Any, Optional

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server

from alice_core.config.personae import Personae, placeholder as placeholder_personae

from ..domain.principals import AddressBook
from ..infra.config import Config
from ..infra.signal_rpc import SignalRPC as SignalClient
from . import config_tools, inner, memory, messaging


SERVER_NAME = "alice"


def build(
    cfg: Config,
    *,
    address_book: AddressBook,
    signal: Optional[SignalClient] = None,
    sender: Optional[messaging.SendCallable] = None,
    personae: Optional[Personae] = None,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Return the mcp_servers dict and the fully-qualified allowed_tools list
    for ClaudeAgentOptions.

    ``signal`` or ``sender`` is optional — when both are omitted the
    send_message tool is skipped (useful for tests and the think-hemisphere
    harness). Daemon callers should pass their own ``sender`` closure so the
    daemon can track whether a turn produced any outbound (missed_reply
    detection).

    ``personae`` is an :class:`alice_core.config.personae.Personae`; its
    agent + user names interpolate into tool descriptions (Plan 05 Phase 5).
    Defaults to the placeholder personae so existing callers (tests, the
    think-hemisphere harness) don't have to load one.
    """
    if personae is None:
        personae = placeholder_personae()
    tool_list: list[Any] = [
        *inner.build(cfg, personae=personae),
        *memory.build(cfg, personae=personae),
        *config_tools.build(cfg, personae=personae),
    ]
    if sender is not None or signal is not None:
        tool_list.extend(
            messaging.build(
                cfg,
                address_book=address_book,
                signal=signal,
                sender=sender,
                personae=personae,
            )
        )
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tool_list)
    # Agent SDK scopes MCP tools as `mcp__<server>__<tool>` in allowed_tools.
    allowed = [f"mcp__{SERVER_NAME}__{t.name}" for t in tool_list]
    return {SERVER_NAME: server}, allowed
