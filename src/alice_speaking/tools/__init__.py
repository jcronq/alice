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
from . import (
    background_task,
    config_tools,
    deploy,
    fs,
    inner,
    memory,
    messaging,
)


SERVER_NAME = "alice"


def _python_type_to_json_schema(py_type: Any) -> dict[str, Any]:
    """Mirror of claude_agent_sdk._python_type_to_json_schema, kept local
    so the probe doesn't reach into the SDK's private API."""
    import typing as _t

    if py_type is str:
        return {"type": "string"}
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    if py_type is list:
        return {"type": "array"}
    if py_type is dict:
        return {"type": "object"}
    origin = _t.get_origin(py_type)
    if origin is list:
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}


def _tool_input_schema(tool_def: Any) -> dict[str, Any]:
    """Reduce an SdkMcpTool's input_schema (which may be a python type-shape
    dict like ``{'q': str}``, a full JSON Schema dict, or a TypedDict) to a
    plain JSON Schema dict that's safe to JSON-serialize for the probe."""
    schema = getattr(tool_def, "input_schema", None)
    if isinstance(schema, dict):
        if (
            "type" in schema
            and "properties" in schema
            and isinstance(schema["type"], str)
        ):
            return schema
        properties = {
            name: _python_type_to_json_schema(t) for name, t in schema.items()
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties.keys()),
        }
    return {"type": "object", "properties": {}}


def _tool_def_dict(tool_def: Any) -> dict[str, Any]:
    return {
        "name": getattr(tool_def, "name", "?"),
        "description": getattr(tool_def, "description", "") or "",
        "input_schema": _tool_input_schema(tool_def),
    }


def build(
    cfg: Config,
    *,
    address_book: AddressBook,
    signal: Optional[SignalClient] = None,
    sender: Optional[messaging.SendCallable] = None,
    personae: Optional[Personae] = None,
    background_dispatcher: Optional[background_task.DispatchCallable] = None,
    background_text_sender: Optional[background_task.TextSenderCallable] = None,
) -> tuple[
    dict[str, McpSdkServerConfig],
    list[str],
    dict[str, list[dict[str, Any]]],
]:
    """Return the mcp_servers dict, the fully-qualified allowed_tools list
    for ClaudeAgentOptions, and a JSON-friendly inventory of every tool's
    full definition (name, description, JSON Schema input).

    The third return value backs the viewer's /context tab — it lets the
    page show the actual schemas Anthropic's harness sees, not just tool
    names.

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
        *fs.build(cfg, personae=personae),
        *deploy.build(cfg, personae=personae),
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
    # Background-task dispatcher is daemon-supplied (closes over the
    # subagent registry + kernel factory). Tests / harnesses without
    # a running daemon can omit it; the tool just isn't exposed.
    if background_dispatcher is not None:
        tool_list.extend(
            background_task.build(
                cfg,
                dispatcher=background_dispatcher,
                text_sender=background_text_sender,
                personae=personae,
            )
        )
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tool_list)
    # Agent SDK scopes MCP tools as `mcp__<server>__<tool>` in allowed_tools.
    allowed = [f"mcp__{SERVER_NAME}__{t.name}" for t in tool_list]
    tool_defs = {SERVER_NAME: [_tool_def_dict(t) for t in tool_list]}
    return {SERVER_NAME: server}, allowed, tool_defs
