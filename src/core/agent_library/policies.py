"""Pre-built :class:`ToolPolicy` instances.

Five policies cover the Phase 1 + Phase 2 flavor set:

* :data:`read_only` — read + grep + glob only. Used by thinking
  sub-agents and code reviewers whose entire job is to summarize /
  cite / verdict without touching the filesystem.
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
* :data:`read_only_with_signal` — Phase 2: watcher policy. Read
  tools + the ``mcp__alice__send_message`` outbound channel and
  nothing else. The watcher flavor (GitHub issue scanners, the
  cortex-memory cue runner) reads context, may notify Jason/Katie,
  but must never mutate the filesystem or shell out. Path-level
  routing of observations through ``inner/notes/`` is enforced via
  :class:`BehavioralRule` in the spec, not at the tool layer.
* :data:`config_writer` — Phase 2: config-worker policy. Read + Bash
  + Edit + Write (no MCP). Sibling to :data:`full_access` but
  without the Signal/experiment MCP surface — config changes
  shouldn't reach for Signal, and shouldn't be running ad-hoc
  experiments. Path-level "config files only" enforcement lives in
  the spec's :class:`BehavioralRule`s.

Adding a new policy = one module-level frozen :class:`ToolPolicy`.
The list of tool names mirrors :data:`_FULL_TOOL_ALLOWLIST` in
:mod:`alice_thinking.runtime` plus the canonical Anthropic SDK
built-ins (``Bash``, ``Read``, etc.). Update both lists when a new
tool ships.
"""

from __future__ import annotations

from .types import ToolPolicy


__all__ = [
    "config_writer",
    "exec_only",
    "full_access",
    "read_only",
    "read_only_with_signal",
]


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


# Read + send_message — watcher policy. No filesystem mutation, no
# bash, no experiment MCP — just enough to read context and ping
# Jason/Katie when something deserves a notification. Observations
# that don't deserve a Signal ping route through ``inner/notes/``
# (the watcher writes those via Read+nothing; the file drop is via
# the dispatcher / supervisor that owns the watcher process).
_READ_WITH_SIGNAL_TOOLS = frozenset(
    _READ_ONLY_TOOLS | {"mcp__alice__send_message"}
)


# Config-worker policy. Read + Bash + Edit + Write, no MCP. Config
# changes are file-only diffs that may need ``git`` / ``yq`` /
# ``python -m json.tool`` to validate, so Bash stays in. Signal is
# out — config changes don't talk to Jason directly; the dispatcher
# audit comment is the trail.
_CONFIG_WRITER_TOOLS = frozenset(
    _EXEC_ONLY_TOOLS | {"Edit", "Write"}
)


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


read_only_with_signal = ToolPolicy(
    type="allow", allowlist=_READ_WITH_SIGNAL_TOOLS
)


config_writer = ToolPolicy(type="allow", allowlist=_CONFIG_WRITER_TOOLS)
