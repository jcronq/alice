"""HermesKernel — :class:`Kernel` impl backed by an OpenAI-compatible
Hermes (NousResearch) endpoint.

One HTTP POST to ``{base_url}/chat/completions`` per :meth:`run`,
end-to-end, with observability. The kernel is the smallest reusable
unit of agent work: given a prompt, a model, a tool allowlist, and
an optional cwd, it makes the request, translates the assistant
response to Alice blocks + handler calls, and returns a
:class:`KernelResult`.

**Scope limitation (v1):** HermesKernel does NOT own the tool-execution
loop. The OpenAI ``/v1/chat/completions`` endpoint is stateless — it
returns ``tool_calls`` and expects the client to execute the tools
and POST a follow-up with ``role="tool"`` messages. AnthropicKernel
and PiKernel delegate that loop to their underlying transports
(claude_agent_sdk / pi subprocess) which own the tool executor.
HermesKernel has no such transport; the current impl emits
``tool_use`` events (via handlers) on any returned ``tool_calls`` but
returns the KernelResult without executing them. A follow-up will
add a tool-loop driver once the executor contract is decided.

Auth: reads ``HERMES_API_KEY`` from ``os.environ`` at request time.
Empty key → request goes out without an ``Authorization`` header
(local vLLM deployments typically don't require auth). Non-empty →
``Authorization: Bearer <key>``.

Endpoint: ``base_url`` from :class:`KernelSpec` when populated (via
``mind/config/model.yml`` ``backends.hermes.base_url``); falls back
to ``HERMES_BASE_URL`` env var, then to the module default
:data:`DEFAULT_BASE_URL` (Nous-hosted).

Timeout: ``spec.max_seconds`` wraps the request via
:func:`asyncio.timeout`. On timeout we emit ``timeout`` and return a
KernelResult with ``error="timeout"`` — mirrors PiKernel and
AnthropicKernel semantics.

Rate limit: HTTP 429 from the endpoint raises
``RuntimeError("hermes rate_limit")`` so callers can implement backoff
identical to the Anthropic path.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from typing import Any, Optional

import httpx

from core.events import EventEmitter
from core.kernel import (
    BlockHandler,
    KernelResult,
    KernelSpec,
    SystemEvent,
    TurnSummary,
    UsageInfo,
)

from .translator import (
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    alice_tools_to_openai,
    openai_message_to_blocks,
    openai_usage_to_info,
)


__all__ = ["HermesKernel"]


# Default endpoint. Nous-hosted per Hermes-kernel design (pin 2).
# Overridden per-run via KernelSpec.base_url (from model.yml) or the
# HERMES_BASE_URL env var. Includes ``/v1`` — kernel appends only
# ``/chat/completions``.
DEFAULT_BASE_URL = "https://api.nousres.org/v1"

# Default request timeout when spec.max_seconds is unset. Generous
# ceiling so a large-context turn doesn't fail on transport timeout;
# spec.max_seconds is the operator-facing knob.
DEFAULT_HTTP_TIMEOUT = 300.0


# KernelSpec fields HermesKernel cannot honor. Mirrors PiKernel's
# ``_PI_UNSUPPORTED_SPEC_FIELDS`` pattern — silent drops are the trap;
# one event per populated field so operators can grep for the drop.
_HERMES_UNSUPPORTED_SPEC_FIELDS: tuple[str, ...] = (
    "hooks",
    "mcp_servers",
    "resume",
    "add_dirs",
    "cwd",
)


def _summarize_dropped_value(value: Any) -> str:
    """Cheap, log-safe summary of a dropped field's value. Mirrors
    PiKernel's helper of the same name."""
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _short(value: Any, cap: int = 2000) -> Any:
    """Mirror of :func:`core.sdk_compat._short` for event payloads.

    Local copy so this module has no core.sdk_compat dependency —
    keeps the hermes package lean and avoids re-import friction
    against the SDK-facing helper.
    """
    try:
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, default=str, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _resolve_base_url(spec: KernelSpec) -> str:
    """Resolve base_url from spec → env → module default."""
    spec_base = getattr(spec, "base_url", "") or ""
    if spec_base:
        return spec_base.rstrip("/")
    env_base = os.environ.get("HERMES_BASE_URL", "") or ""
    if env_base:
        return env_base.rstrip("/")
    return DEFAULT_BASE_URL


def _resolve_api_key() -> str:
    """Read HERMES_API_KEY at request time — env changes take effect
    without a module reload (matches PiKernel's ALICE_PI_BIN pattern)."""
    return os.environ.get("HERMES_API_KEY", "") or ""


class HermesKernel:
    """Drive one Hermes chat-completions call to completion.
    Implements :class:`Kernel`."""

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
        """Emit ``hermes_spec_field_dropped`` once per KernelSpec field
        HermesKernel cannot honor. Mirrors PiKernel's
        ``pi_spec_field_dropped`` — silent drops mask real config bugs
        (an operator wiring ``mcp_servers`` and wondering why the tool
        never fires). One event per populated field gives the next
        victim something to grep for.
        """
        for field_name in _HERMES_UNSUPPORTED_SPEC_FIELDS:
            value = getattr(spec, field_name, None)
            if value is None or value == "" or value == {} or value == [] or value == ():
                continue
            self._emit(
                "hermes_spec_field_dropped",
                field=field_name,
                value_summary=_summarize_dropped_value(value),
            )

    async def run(
        self,
        prompt: str,
        spec: KernelSpec,
        handlers: Optional[list[BlockHandler]] = None,
    ) -> KernelResult:
        # Warn loudly about KernelSpec fields HermesKernel cannot
        # honor. Fires BEFORE the request so an operator whose config
        # is subtly wrong sees the drop event even if the request
        # subsequently errors out on the wire.
        self._warn_unsupported_fields(spec)

        handlers = list(handlers or [])
        request = self._build_request(prompt, spec)
        endpoint = f"{_resolve_base_url(spec)}/chat/completions"
        headers = self._build_headers()

        # Session id: OpenAI-compat responses don't carry one, so we
        # synthesize a locally-unique id at request time so downstream
        # consumers (viewer session grouping) always have a key. Format
        # ``hermes-<ns>`` matches the ``<backend>-<uniq>`` convention.
        start_ns = time.monotonic_ns()
        session_id = f"hermes-{start_ns}"

        parts: list[str] = []
        usage_info: Optional[UsageInfo] = None
        cost_usd: Optional[float] = None
        is_error = False
        duration_ms: Optional[int] = None
        raw_response: Optional[dict] = None

        try:
            if spec.max_seconds and spec.max_seconds > 0:
                async with asyncio.timeout(spec.max_seconds):
                    raw_response = await self._post(endpoint, request, headers)
            else:
                raw_response = await self._post(endpoint, request, headers)
        except asyncio.TimeoutError:
            self._emit("timeout", max_seconds=spec.max_seconds)
            return KernelResult(
                text="".join(parts),
                session_id=session_id,
                usage=usage_info,
                duration_ms=None,
                cost_usd=cost_usd,
                is_error=True,
                num_turns=None,
                error="timeout",
            )

        duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)

        # System event: minimal record of the transport session so
        # viewer / event-log consumers can identify hermes turns.
        self._emit(
            "system",
            subtype="hermes.session",
            data={
                "id": session_id,
                "model": spec.model,
                "endpoint": endpoint,
            },
        )
        sysev = SystemEvent(
            subtype="hermes.session",
            data={"id": session_id, "model": spec.model, "endpoint": endpoint},
        )
        for h in handlers:
            await h.on_system(sysev)

        choices = raw_response.get("choices") if isinstance(raw_response, dict) else None
        if not isinstance(choices, list) or not choices:
            # Malformed response — surface as error rather than empty
            # KernelResult so the caller's retry logic engages.
            self._emit("hermes_bad_response", detail=_short(raw_response, self._cap))
            return KernelResult(
                text="",
                session_id=session_id,
                usage=None,
                duration_ms=duration_ms,
                cost_usd=None,
                is_error=True,
                num_turns=0,
                error="bad_response",
            )

        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        blocks = openai_message_to_blocks(message)

        for block in blocks:
            await self._dispatch_block(block, parts, handlers)

        usage_info = openai_usage_to_info(raw_response.get("usage"))

        # Emit + fan out the terminal result event, matching the
        # AnthropicKernel / PiKernel shape so viewer aggregators
        # process both backends identically.
        self._emit(
            "result",
            session_id=session_id,
            num_turns=1,
            duration_ms=duration_ms,
            total_cost_usd=cost_usd,
            is_error=is_error,
            usage=_usage_info_to_event_dict(usage_info),
        )
        summary = TurnSummary(
            session_id=session_id,
            usage=usage_info,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            is_error=is_error,
            num_turns=1,
            raw=raw_response,
        )
        for h in handlers:
            await h.on_result(summary)

        return KernelResult(
            text="".join(parts).strip(),
            session_id=session_id,
            usage=usage_info,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            is_error=is_error,
            num_turns=1,
        )

    async def _dispatch_block(
        self,
        block: Any,
        parts: list[str],
        handlers: list[BlockHandler],
    ) -> None:
        if isinstance(block, TextBlock):
            parts.append(block.text)
            self._emit("assistant_text", text=_short(block.text, self._cap))
            for h in handlers:
                await h.on_text(block.text)
        elif isinstance(block, ToolUseBlock):
            self._emit(
                "tool_use",
                name=block.name,
                input=_short(block.input, self._cap),
                id=block.id,
            )
            for h in handlers:
                await h.on_tool_use(block.name, block.input, block.id)
        elif isinstance(block, ThinkingBlock):
            self._emit("thinking", text=_short(block.thinking, self._cap))
            for h in handlers:
                await h.on_thinking(block.thinking)

    def _build_request(self, prompt: str, spec: KernelSpec) -> dict:
        """Construct the OpenAI /v1/chat/completions request body."""
        messages: list[dict[str, Any]] = []
        if spec.append_system_prompt:
            messages.append(
                {"role": "system", "content": spec.append_system_prompt}
            )
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": spec.model,
            "messages": messages,
            "stream": False,
        }

        tools = alice_tools_to_openai(spec.allowed_tools or [])
        if tools:
            body["tools"] = tools

        return body

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        key = _resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def _post(
        self,
        endpoint: str,
        body: dict,
        headers: dict[str, str],
    ) -> dict:
        """POST to the Hermes endpoint; return parsed JSON. Errors
        raise ``RuntimeError`` with a shape callers can pattern-match
        (``"hermes rate_limit"`` for 429, ``"hermes error: ..."`` for
        other HTTP faults).
        """
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
                response = await client.post(endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"hermes transport error: {exc}") from exc

        if response.status_code == 429:
            raise RuntimeError("hermes rate_limit")
        if response.status_code >= 400:
            snippet = response.text[:500] if hasattr(response, "text") else ""
            raise RuntimeError(
                f"hermes error: HTTP {response.status_code}: {snippet}"
            )

        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"hermes error: non-JSON response: {exc}") from exc


def _usage_info_to_event_dict(usage: Optional[UsageInfo]) -> Optional[dict]:
    """Serialize :class:`UsageInfo` for the JSONL event log so
    aggregators (which expect Anthropic-shaped keys) keep working."""
    if usage is None:
        return None
    return dataclasses.asdict(usage)
