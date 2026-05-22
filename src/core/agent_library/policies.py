"""Pre-built :class:`ToolPolicy` instances.

Three policies cover the common Phase 1 cases:

* :data:`read_only` — read + grep + glob only. Used by thinking
  sub-agents whose entire job is to summarize / cite without
  touching the filesystem.
* :data:`exec_only` — read + bash, no file writes. Useful for
  diagnostic / probe agents that need to run commands but must
  not modify the working tree.
* :data:`full_access` — allow-everything sentinel. The policy
  itself enforces nothing — its ``allowlist`` is intentionally
  broad. The reason it exists at all is so an agent spec can
  declare "yes, I considered tool scoping, and the answer is
  unrestricted" rather than leaving :attr:`AgentSpec.tool_policy`
  as ``None`` (which conflates "no policy declared" with "deliberate
  open access").

Adding a new policy = one module-level frozen :class:`ToolPolicy`.
The list of tool names mirrors :data:`_FULL_TOOL_ALLOWLIST` in
:mod:`alice_thinking.runtime` plus the canonical Anthropic SDK
built-ins (``Bash``, ``Read``, etc.). Update both lists when a new
tool ships.
"""

from __future__ import annotations

from .types import ToolPolicy


__all__ = ["exec_only", "full_access", "read_only"]


# Read-side tools — pure inspection, no filesystem or network writes.
_READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
    }
)


# Read + Bash — diagnostic agents can probe a running system but must
# not commit changes. Edit/Write deliberately excluded.
_EXEC_ONLY_TOOLS = frozenset(_READ_ONLY_TOOLS | {"Bash"})


# Broad set covering the SDK built-ins plus the MCP tools threaded
# through thinking's wake (kept in sync with
# :data:`alice_thinking.runtime._FULL_TOOL_ALLOWLIST`).
_FULL_ACCESS_TOOLS = frozenset(
    _EXEC_ONLY_TOOLS
    | {
        "Edit",
        "Write",
        "mcp__alice__send_message",
        "mcp__alice__run_experiment",
    }
)


read_only = ToolPolicy(type="allow", allowlist=_READ_ONLY_TOOLS)


exec_only = ToolPolicy(type="allow", allowlist=_EXEC_ONLY_TOOLS)


full_access = ToolPolicy(type="allow", allowlist=_FULL_ACCESS_TOOLS)
