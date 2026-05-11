"""Thinking-side MCP tools.

Speaking-side tools live in :mod:`alice_speaking.tools`. Thinking has its
own (smaller) tool set, exposed via a separate MCP server (``alice``)
threaded into the wake's :class:`KernelSpec` so the kernel passes the
right MCP config to the SDK.

Today the thinking-side server exposes exactly one tool —
``run_experiment`` (commissioned 2026-05-11 per
``inner/notes/2026-05-11-115827-design-proposal.md``). The package is
structured so additional thinking-only tools (`request_worker_reload`,
`resolve_surface`, etc.) can plug in alongside without rewiring the
kernel-spec composition.

The server name matches the speaking-side ``alice`` server: tools are
namespaced as ``mcp__alice__<tool>``. The mind-aware MCP servers (notes,
memory, config) are speaking-only — thinking sees the world through its
prompt + the kernel's tools, not through filesystem-mutating MCP. The
single exception is ``run_experiment``, which is structurally identical
to a speaking-side dispatch.
"""

from __future__ import annotations

from typing import Any, Optional

from alice_core.events import EventEmitter

from .run_experiment import build_run_experiment_tool


__all__ = [
    "SERVER_NAME",
    "build",
    "build_run_experiment_tool",
]


SERVER_NAME = "alice"


def build(
    *,
    emitter: EventEmitter,
    runner: Optional[Any] = None,
    api_key: Optional[str] = None,
    api_base_url: Optional[str] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the thinking-side MCP server config + allowed-tools list.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` so the wake-side
    glue can drop them onto the :class:`KernelSpec`. The structure
    mirrors :func:`alice_speaking.tools.build` exactly so the kernel
    treats both hemispheres the same.

    ``runner`` is the :class:`alice_thinking.experiments.ExperimentRunner`
    the tool dispatches to. If not provided the tool constructs a default
    runner using the supplied emitter + auth — convenient for ad-hoc
    wakes but production deploys should pass a pre-built runner so the
    runner's bookkeeping (live-task tracking) survives across multiple
    tool invocations within the same wake.
    """
    from claude_agent_sdk import create_sdk_mcp_server

    tool_obj = build_run_experiment_tool(
        emitter=emitter,
        runner=runner,
        api_key=api_key,
        api_base_url=api_base_url,
    )
    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.1.0",
        tools=[tool_obj],
    )
    allowed = [f"mcp__{SERVER_NAME}__{tool_obj.name}"]
    return {SERVER_NAME: server}, allowed
