"""PiKernel — :class:`Kernel` impl backed by ``pi --mode json``.

Subprocess-and-JSONL wrapper around pi-coding-agent (Mario Zechner's
Node binary). Architecturally analogous to ``claude_agent_sdk``'s
own subprocess transport — same shape, different binary, different
event vocabulary.

Auth: pi reads ``~/.pi/agent/auth.json``. The container entrypoint
runs the codex→pi bridge to populate that file from
``~/.codex/auth.json``; PiKernel itself doesn't touch auth.

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
from typing import Any, Optional

from alice_core.events import EventEmitter
from alice_core.kernel import (
    BlockHandler,
    KernelResult,
    KernelSpec,
    ThinkingLevel,
)

from . import transport as _transport_mod
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

    async def run(
        self,
        prompt: str,
        spec: KernelSpec,
        handlers: Optional[list[BlockHandler]] = None,
    ) -> KernelResult:
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
        # module reload of alice_pi.kernel.
        argv: list[str] = [
            _transport_mod.pi_bin(),
            "--mode", "json",
            "-p", prompt,
            "--no-session",        # Alice owns session state; not pi
            "--no-skills",         # disable directory-based discovery
        ]
        # Skill discovery: explicit --skill <rendered_dir> beats
        # pi's auto-discovery from cwd's .claude/skills (which would
        # find the same files, but being explicit avoids surprises
        # if cwd ever drifts from skills_cwd).
        if spec.cwd is not None:
            skills_dir = spec.cwd / ".claude" / "skills"
            if skills_dir.is_dir():
                argv.extend(["--skill", str(skills_dir)])

        if spec.allowed_tools:
            argv.extend(["--tools", ",".join(spec.allowed_tools)])

        argv.extend(["--model", _normalize_pi_model(spec.model)])
        argv.extend(["--thinking", _thinking_to_pi_arg(spec.thinking)])

        if spec.append_system_prompt:
            argv.extend(["--append-system-prompt", spec.append_system_prompt])

        # add_dirs: pi's --add-dir grants additional read access.
        if spec.add_dirs:
            for path in spec.add_dirs:
                argv.extend(["--add-dir", str(path)])

        # mcp_servers (Anthropic-only): silently ignored — pi has
        # no built-in MCP. Documented in the spike report.
        return argv
