"""Run NousResearch Hermes Agent as a one-shot subprocess with stream-json output.

Requires a separately installed `hermes` CLI (override with ALICE_HERMES_BIN).
A custom OpenAI-compatible endpoint is mandatory: this keeps comparisons on the
same model as pi and prevents accidental calls to Hermes' default paid provider.
The operator supplies it with backends.pi.base_url in model.yml.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from typing import Optional

from core.events import EventEmitter
from core.kernel import BlockHandler, KernelResult, KernelSpec, SystemEvent, TurnSummary, UsageInfo


class HermesAgentKernel:
    def __init__(self, emitter: EventEmitter, *, correlation_id: Optional[str] = None,
                 silent: bool = False, short_cap: int = 2000, base_url: str = "") -> None:
        self.emitter = emitter
        self.correlation_id = correlation_id
        self.silent = silent
        self.cap = short_cap
        self.base_url = base_url

    def _emit(self, event: str, **fields: object) -> None:
        if not self.silent:
            if self.correlation_id:
                fields.setdefault("turn_id", self.correlation_id)
            self.emitter.emit(event, **fields)

    async def run(self, prompt: str, spec: KernelSpec,
                  handlers: Optional[list[BlockHandler]] = None) -> KernelResult:
        if not self.base_url:
            raise ValueError("hermes-agent requires backends.pi.base_url (same inference endpoint as pi)")
        # Hermes cannot call Alice's outbox or MCP bridge. Do not silently let
        # Speaking lose the only way to reply on Signal.
        if "mcp__alice__send_message" in spec.allowed_tools:
            raise ValueError("hermes-agent speaking is not supported: send_message has no Hermes tool bridge")
        handlers = list(handlers or [])
        for field in ("mcp_servers", "hooks", "add_dirs", "permission_mode"):
            if getattr(spec, field, None):
                self._emit("hermes_agent_spec_field_dropped", field=field)
        # Hermes' built-in agent has its own tool vocabulary; there is no
        # one-to-one Alice tool allowlist. Treat unfamiliar MCP tools as drops.
        for name in spec.allowed_tools:
            if name.startswith("mcp__"):
                self._emit("hermes_agent_tool_unavailable", name=name)
        model = spec.model.split("/", 1)[-1]
        query = (f"{spec.append_system_prompt}\n\n{prompt}" if spec.append_system_prompt else prompt)
        argv = [os.environ.get("ALICE_HERMES_BIN", "hermes"), "chat", "--query-file", "-",
                "--oneshot", "--format", "stream-json", "--ignore-user-config",
                "--ignore-rules", "--provider", "custom", "--base-url", self.base_url,
                "--model", model]
        # The gateway transport rejects --run-budget; enforce the wall-clock
        # budget below with asyncio.timeout instead.
        if spec.resume:
            argv += ["--resume", spec.resume]
        cwd = str(spec.cwd) if spec.cwd else None
        env = os.environ.copy()
        # LiteLLM requires its master key. Hermes resolves OPENAI_API_KEY for
        # custom OpenAI-compatible endpoints; do not place secrets in argv.
        if (self.base_url.rstrip("/") == env.get("LITELLM_BASE_URL", "").rstrip("/")
                and env.get("LITELLM_MASTER_KEY")):
            env["OPENAI_BASE_URL"] = self.base_url
            env["OPENAI_API_KEY"] = env["LITELLM_MASTER_KEY"]
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=10 * 1024 * 1024)
        except FileNotFoundError as exc:
            raise RuntimeError("Hermes Agent CLI not installed (set ALICE_HERMES_BIN)") from exc
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(query.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(proc.stderr.read())
        session_id = None
        result_event = None
        text_parts: list[str] = []
        try:
            async with asyncio.timeout(spec.max_seconds if spec.max_seconds > 0 else None):
                async for raw in proc.stdout:
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise RuntimeError("Hermes Agent emitted non-JSON stdout in stream-json mode")
                    kind = event.get("type")
                    if kind == "system":
                        session_id = event.get("session_id") or session_id
                        self._emit("system", subtype="hermes_agent.init", data=event)
                        for h in handlers:
                            await h.on_system(SystemEvent(subtype="hermes_agent.init", data=event))
                    elif kind == "text":
                        delta = event.get("text", "")
                        text_parts.append(delta)
                        self._emit("assistant_text", text=delta[:self.cap])
                        for h in handlers:
                            await h.on_text(delta)
                    elif kind == "tool_use":
                        name, args, tool_id = event.get("name", ""), event.get("input") or {}, event.get("tool_call_id", "")
                        self._emit("tool_use", name=name, input=args, id=tool_id)
                        for h in handlers:
                            await h.on_tool_use(name, args, tool_id)
                    elif kind == "tool_result":
                        name, output, tool_id = event.get("name", ""), event.get("output", ""), event.get("tool_call_id", "")
                        self._emit("tool_result", name=name, output=str(output)[:self.cap], id=tool_id)
                        for h in handlers:
                            await h.on_tool_result(tool_id, output, bool(event.get("is_error")))
                    elif kind == "result":
                        if result_event is not None:
                            raise RuntimeError("Hermes Agent emitted multiple terminal results")
                        result_event = event
                        session_id = event.get("session_id") or session_id
                rc = await proc.wait()
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        finally:
            stderr = (await stderr_task).decode(errors="replace")
        if result_event is None:
            raise RuntimeError(f"Hermes Agent exited {rc} without result: {stderr[:500]}")
        tokens = result_event.get("tokens") or {}
        usage = UsageInfo(input_tokens=int(tokens.get("input") or 0),
                          output_tokens=int(tokens.get("output") or 0),
                          cache_read_input_tokens=int(tokens.get("cache_read") or 0),
                          cache_creation_input_tokens=int(tokens.get("cache_write") or 0),
                          total_tokens=int(tokens.get("total") or 0))
        failed = bool(rc or result_event.get("exit_code") or result_event.get("error"))
        text = result_event.get("text") or "".join(text_parts)
        self._emit("result", session_id=session_id, duration_ms=result_event.get("duration_ms"),
                   num_turns=1, is_error=failed, usage=dataclasses.asdict(usage))
        summary = TurnSummary(session_id=session_id, usage=usage,
                              duration_ms=result_event.get("duration_ms"), cost_usd=None,
                              is_error=failed, num_turns=1, raw=result_event)
        for h in handlers:
            await h.on_result(summary)
        return KernelResult(text=text, session_id=session_id, usage=usage,
                            duration_ms=result_event.get("duration_ms"), cost_usd=None,
                            is_error=failed, num_turns=1,
                            error=(result_event.get("error") or f"exit {rc}") if failed else None)
