"""Built-in :class:`AgentSpec` registrations.

Imported for its side effect: every registration calls
:meth:`Registry.register` on :data:`default_registry` at module-import
time. Phase 1 ships two entries — ``thinking`` and ``speaking`` —
matching the design synthesis note. Phase 2 adds the remaining six
flavors (reviewer, designer, watchers, ...).

The :class:`KernelSpec` templates here are deliberately conservative
defaults: model + tool list + max_seconds that match today's
in-process invocations. Per-call overrides remain the caller's job
(via :func:`dataclasses.replace` on the spec before
:meth:`AgentSpec.build_spec`); Phase 2 lifts that into a richer
runtime override mechanism.
"""

from __future__ import annotations

from ..kernel import KernelSpec
from . import policies
from .registry import default_registry
from .types import AgentSpec, BehavioralRule


__all__ = ["register_builtins"]


# Default model for both hemispheres — matches today's
# alice.config.json fallback (the real config can still override at
# call time by passing a different KernelSpec into :func:`run_agent`).
_DEFAULT_MODEL = "claude-opus-4-7"


_THINKING_RULES = (
    BehavioralRule(
        id="no-real-world-writes",
        injection=(
            "You may only read and write files inside ~/alice-mind/. "
            "Never modify the host filesystem, never install packages, "
            "never run destructive shell commands. Escalate any "
            "real-world change via inner/surface/."
        ),
    ),
    BehavioralRule(
        id="no-direct-signal",
        injection=(
            "Do not send Signal messages directly. If something needs "
            "to reach Jason or Katie, drop a fleeting note in "
            "inner/notes/ and let speaking decide whether to voice it."
        ),
    ),
    BehavioralRule(
        id="design-not-build",
        injection=(
            "Your job is to think, design, and groom — not to build. "
            "When a note surfaces a problem that needs code, write the "
            "proposal in the vault and let speaking dispatch the build."
        ),
    ),
)


_SPEAKING_RULES = (
    BehavioralRule(
        id="ask-before-external-action",
        injection=(
            "Ask Jason before taking actions that leave the machine and "
            "were not explicitly requested: emails, public posts, "
            "third-party uploads, irreversible config changes."
        ),
    ),
    BehavioralRule(
        id="signal-via-send-message",
        injection=(
            "Reach Signal only through the send_message MCP tool. Never "
            "shell out to signal-cli, notify, or curl — the daemon "
            "wraps quiet-hours + surface handling around send_message."
        ),
    ),
)


def _thinking_template() -> KernelSpec:
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
            "mcp__alice__send_message",
            "mcp__alice__run_experiment",
        ],
        max_seconds=0,
        thinking="medium",
    )


def _speaking_template() -> KernelSpec:
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
        ],
        max_seconds=0,
        thinking=None,
    )


def register_builtins(registry=default_registry) -> None:
    """Register the Phase 1 built-in agents.

    Idempotent for the default singleton (re-registration is a no-op
    when the names are already present). Tests can pass a fresh
    :class:`Registry` to get an isolated population.
    """
    for spec in (_thinking_spec(), _speaking_spec()):
        if spec.name in registry:
            continue
        registry.register(spec)


def _thinking_spec() -> AgentSpec:
    return AgentSpec(
        name="thinking",
        persona="thinking",
        kernel_spec=_thinking_template(),
        runtime="claude-agent-sdk",
        scope="background",
        lifecycle="always-on",
        tool_policy=policies.full_access,
        behavioral_constraints=_THINKING_RULES,
    )


def _speaking_spec() -> AgentSpec:
    return AgentSpec(
        name="speaking",
        persona="speaking",
        kernel_spec=_speaking_template(),
        runtime="claude-agent-sdk",
        scope="conversational",
        lifecycle="always-on",
        tool_policy=policies.full_access,
        behavioral_constraints=_SPEAKING_RULES,
    )


# Populate the module-level singleton on import. Tests that need an
# empty registry build their own :class:`Registry` instance.
register_builtins()
