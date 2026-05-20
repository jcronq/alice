"""PiKernel — :class:`Kernel` impl backed by ``pi --mode json``.

Subprocess-and-JSONL wrapper around pi-coding-agent (Mario Zechner's
Node binary). Architecturally analogous to ``claude_agent_sdk``'s
own subprocess transport — same shape, different binary, different
event vocabulary.

Auth: pi reads ``~/.pi/agent/auth.json``. The container entrypoint
runs the codex→pi bridge to populate that file from
``~/.codex/auth.json``; PiKernel itself doesn't touch auth.

Models registry: pi reads ``~/.pi/agent/models.json`` for its
provider/model list. Alice's source-of-truth registry lives in
the vault at ``~/alice-mind/config/pi-models.json``. PiKernel.run()
stages vault → pi runtime location via
:func:`kernels.pi.models_staging.ensure_pi_models_json` at the start
of every run. Idempotent (skips when content matches) and
fail-soft (warning event, no raise) — pi can always fall back to
its built-in providers.

Skills: PiKernel passes ``--skill <rendered_dir>`` (the per-hemisphere
ephemeral skills dir from Plan 07 P3). Pi auto-discovery falls back
to ``.claude/skills/`` under cwd as well; we set cwd to the same
dir to be defensive.

Compaction: Alice owns compaction. ``--no-session`` (and pi's own
``compaction.enabled: false`` setting) keeps pi from rolling its
own context.
"""

from __future__ import annotations

import asyncio
from importlib import resources
from typing import Any, Optional

from core.events import EventEmitter
from core.kernel import (
    BlockHandler,
    KernelResult,
    KernelSpec,
    ThinkingLevel,
)

from . import transport as _transport_mod
from .models_staging import ensure_pi_models_json
from .transport import stream_pi_events
from .translator import PiEventTranslator


__all__ = ["PiKernel"]


def _thinking_to_pi_arg(level: Optional[ThinkingLevel]) -> str:
    if level is None or level == "off":
        return "off"
    return level


def _normalize_pi_model(model: str) -> str:
    """If the operator wrote ``gpt-5.3-codex`` (no provider prefix),
    prepend ``openai-codex/`` since "pi" backend implies the Codex
    subscription provider. Power users can override by writing
    ``<provider>/<model>`` directly in model.yml."""
    if "/" in model:
        return model
    return f"openai-codex/{model}"


# Map Alice's Claude-Code-style tool names to pi-coding-agent's
# lowercase tool names. WebFetch / WebSearch have no pi equivalent
# and get dropped silently (pi extensions can add them later).
_PI_TOOL_NAME_MAP: dict[str, Optional[str]] = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Grep": "grep",
    "Glob": "find",  # closest pi equivalent
    "LS": "ls",
    "Ls": "ls",
    "WebFetch": None,  # not available in pi
    "WebSearch": None,  # not available in pi
    # Alice's speaking outbox is MCP-backed in Claude Code. Pi has no
    # MCP client, so PiKernel loads a tiny native extension with the same
    # tool name and TurnRunner handles the emitted tool call.
    "mcp__alice__send_message": "send_message",
}


# KernelSpec fields PiKernel cannot honor (Anthropic-SDK-only) and
# silently drops at argv-construction time. Listed here so the
# preflight check in :meth:`PiKernel.run` can warn the operator
# whenever a caller actually populates one — masking these on the
# pi backend is exactly the trap that hid the ``run_experiment``
# MCP-tool wiring bug from us for days. New Anthropic-only fields
# added to :class:`KernelSpec` should be appended here.
_PI_UNSUPPORTED_SPEC_FIELDS: tuple[str, ...] = ("mcp_servers", "hooks")


def _summarize_dropped_value(value: Any) -> str:
    """Cheap, log-safe summary of a dropped field's value.

    Goal is "enough to identify what the operator passed" without
    dumping a 50KB hook table or an MCP server config into the event
    log. Collections report their length; everything else reports its
    type name.
    """
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _translate_tools(allowed: list[str]) -> list[str]:
    """Translate Claude tool names to pi names. Unknown names pass
    through lowercased so custom/extension tools (which the
    operator wrote in their pi-native form) still work. Returns
    [] when every requested tool dropped — caller treats that as
    "fall back to pi's default tool set"."""
    out: list[str] = []
    for name in allowed:
        if name in _PI_TOOL_NAME_MAP:
            mapped = _PI_TOOL_NAME_MAP[name]
            if mapped is not None:
                out.append(mapped)
            continue
        if name.startswith("mcp__"):
            continue
        # Unknown name: lowercased pass-through. Either it's already
        # a pi-native name, or pi will reject it and the operator
        # gets a clear error.
        out.append(name.lower())
    return out


class PiKernel:
    """Drive one pi session to completion. Implements :class:`Kernel`."""

    def __init__(
        self,
        emitter: EventEmitter,
        *,
        correlation_id: Optional[str] = None,
        silent: bool = False,
        short_cap: int = 2000,
    ) -> None:
        self.emitter = emitter
        self.correlation_id = correlation_id
        self.silent = silent
        self._cap = short_cap

    def _emit(self, event: str, **fields: Any) -> None:
        if self.silent:
            return
        if self.correlation_id is not None:
            fields.setdefault("turn_id", self.correlation_id)
        self.emitter.emit(event, **fields)

    def _warn_unsupported_fields(self, spec: KernelSpec) -> None:
        """Emit ``pi_spec_field_dropped`` once per Anthropic-only field
        the caller populated.

        PiKernel silently ignores fields like ``mcp_servers`` and
        ``hooks`` because pi has no equivalent — but "silently" is
        the trap. The ``run_experiment`` MCP tool was wired into
        ``mcp_servers``, PiKernel ignored it, no signal surfaced, and
        thinking failed to call the tool for days. One event per
        populated drop-field gives the next victim something to grep
        for. See also :data:`_PI_UNSUPPORTED_SPEC_FIELDS`.
        """
        for field_name in _PI_UNSUPPORTED_SPEC_FIELDS:
            value = getattr(spec, field_name, None)
            if value is None or value == {} or value == [] or value == ():
                continue
            self._emit(
                "pi_spec_field_dropped",
                field=field_name,
                value_summary=_summarize_dropped_value(value),
            )

    async def run(
        self,
        prompt: str,
        spec: KernelSpec,
        handlers: Optional[list[BlockHandler]] = None,
    ) -> KernelResult:
        # Warn loudly about KernelSpec fields PiKernel cannot honor.
        # Must fire BEFORE argv translation (and before the model
        # registry stage) so an operator who passed e.g. an MCP tool
        # config sees the drop event even if a subsequent stage
        # raises.
        self._warn_unsupported_fields(spec)
        # Stage the alice-managed pi model registry before pi reads
        # ``~/.pi/agent/models.json``. Idempotent + fail-soft — never
        # blocks the run, just emits a warning event on failure.
        ensure_pi_models_json(emit=self._emit)

        handlers = list(handlers or [])
        argv = self._build_argv(prompt, spec)
        translator = PiEventTranslator(self._emit, short_cap=self._cap)

        try:
            if spec.max_seconds and spec.max_seconds > 0:
                async with asyncio.timeout(spec.max_seconds):
                    await self._drive(argv, spec, translator, handlers)
            else:
                await self._drive(argv, spec, translator, handlers)
        except asyncio.TimeoutError:
            self._emit("timeout", max_seconds=spec.max_seconds)
            return translator.to_kernel_result(error="timeout", is_error=True)

        return translator.to_kernel_result()

    async def _drive(
        self,
        argv: list[str],
        spec: KernelSpec,
        translator: PiEventTranslator,
        handlers: list[BlockHandler],
    ) -> None:
        cwd = str(spec.cwd) if spec.cwd is not None else None
        async for event in stream_pi_events(argv, cwd=cwd):
            await translator.handle(event, handlers)

    def _build_argv(self, prompt: str, spec: KernelSpec) -> list[str]:
        # Read PI_BIN dynamically so test fixtures + runtime env
        # changes (ALICE_PI_BIN) take effect without needing a
        # module reload of kernels.pi.kernel.
        argv: list[str] = [
            _transport_mod.pi_bin(),
            "--mode",
            "json",
            "-p",
            prompt,
            "--no-session",  # Alice owns session state; not pi
            "--no-skills",  # disable directory-based discovery
        ]
        # Skill discovery: explicit --skill <rendered_dir> beats
        # pi's auto-discovery from cwd's .claude/skills (which would
        # find the same files, but being explicit avoids surprises
        # if cwd ever drifts from skills_cwd).
        if spec.cwd is not None:
            skills_dir = spec.cwd / ".claude" / "skills"
            if skills_dir.is_dir():
                argv.extend(["--skill", str(skills_dir)])

        # Translate Alice's Claude-Code-style tool names ("Bash",
        # "Read", ...) to pi's lowercase set ("bash", "read", ...).
        # If translation drops every name (e.g. all-WebFetch list)
        # don't pass --tools at all — that lets pi default to its
        # full built-in set rather than running with zero tools.
        translated = _translate_tools(spec.allowed_tools or [])
        if "send_message" in translated:
            argv.extend(["--extension", _send_message_extension_path()])
        if translated:
            argv.extend(["--tools", ",".join(translated)])

        argv.extend(["--model", _normalize_pi_model(spec.model)])
        argv.extend(["--thinking", _thinking_to_pi_arg(spec.thinking)])

        if spec.append_system_prompt:
            argv.extend(["--append-system-prompt", spec.append_system_prompt])

        # add_dirs: silently ignored. Anthropic's claude_agent_sdk
        # uses ``add_dirs`` to grant the agent extra read access
        # beyond cwd, but pi has no equivalent flag — its tools
        # default to whole-filesystem read access from the user
        # account, so skill bodies referencing absolute paths
        # (e.g. ~/alice-mind/...) still resolve via Read/Bash.
        # mcp_servers + hooks (Anthropic-only): also dropped, but
        # NOT silently — PiKernel.run emits one
        # ``pi_spec_field_dropped`` event per populated field so the
        # operator notices the trap (see
        # :meth:`_warn_unsupported_fields` and
        # :data:`_PI_UNSUPPORTED_SPEC_FIELDS`).
        return argv


def _send_message_extension_path() -> str:
    return str(resources.files("kernels.pi").joinpath("extensions", "send-message.js"))
