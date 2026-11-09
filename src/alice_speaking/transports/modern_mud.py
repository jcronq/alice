"""ModernMudTransport: receive ``/Alice`` commands from the modern-mud game.

Wire shape
==========

Inbound (game -> daemon):

  ``POST /api/modern-mud/turn``
      Headers: ``X-Modern-Mud-Key: <shared secret>``
      Body: ``{"user_id": "...", "session_id": "...", "message": "..."}``
      Replies ``202 {"turn_id": ...}`` once the message is accepted into
      the dispatcher queue, ``401`` on a bad/missing key, ``403`` for a
      non-admin ``user_id``, ``400`` on a malformed body, ``503`` when
      the queue is full.

Outbound (daemon -> game):

  ``POST <callback_url>`` with ``{"turn_id", "text"}`` on success or
  ``{"turn_id", "error"}`` on failure, signed with the same shared key
  (``X-Modern-Mud-Key`` plus ``X-Modern-Mud-Signature``, an HMAC-SHA256
  hex digest of the raw body). One block per turn; no streaming.

The shared key is read from a file (default ``/srv/config/modern-mud-key``),
never hardcoded.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ._agui import parse_run_agent_input
from ._agui_router import AGUIRouter, AgentRunner, echo_runner
from .base import (
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)

KEY_HEADER = "X-Modern-Mud-Key"
SIGNATURE_HEADER = "X-Modern-Mud-Signature"
DEFAULT_KEY_PATH = "/srv/config/modern-mud-key"

# Plain text block rendered into a game text area: no markdown.
MODERN_MUD_CAPS = Capabilities(
    markdown="none",
    code_blocks=False,
    images_outbound=False,
    files_outbound=False,
    max_message_bytes=200_000,
    long_message_strategy="split",
    typing_indicator=False,
    reactions=False,
    interactive=True,
    lifecycle_events=False,
)


def load_key(path: str = DEFAULT_KEY_PATH) -> str:
    """Return the shared key from ``path`` or ``""`` when unreadable."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def sign(key: str, body: bytes) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


@dataclass
class ModernMudEvent:
    """An inbound modern-mud message wrapped for the dispatcher."""

    message: InboundMessage


# Callback sender signature: (url, body_bytes, headers) -> awaitable.
CallbackSender = Callable[[str, bytes, dict], Awaitable[None]]


async def _httpx_post(url: str, body: bytes, headers: dict) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, content=body, headers=headers)
        resp.raise_for_status()


class ModernMudTransport:
    name = "modern-mud"
    caps: Capabilities = MODERN_MUD_CAPS
    event_type = ModernMudEvent

    def __init__(
        self,
        *,
        key: str,
        admin_user_ids: set[str],
        callback_url: str,
        principal_name: str = "jason",
        principal_display_name: str = "Jason",
        host: str = "0.0.0.0",
        port: int = 8182,
        inbox_size: int = 64,
        callback_sender: CallbackSender = _httpx_post,
        agui_runner: Optional[AgentRunner] = None,
    ) -> None:
        self._key = key
        self._admin_user_ids = set(admin_user_ids)
        self._callback_url = callback_url
        self._principal_name = principal_name
        self._principal_display_name = principal_display_name
        self._host = host
        self._port = port
        self._send_callback = callback_sender
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        # turn_id -> text chunks emitted by Alice during the turn.
        self._pending: dict[str, list[str]] = {}
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None
        # AG-UI router for /alice/run + /alice/tool-result. The runner
        # is injectable so production can wire the real LLM-driven
        # loop while tests pin a scripted sequence (see
        # ``tests/test_modern_mud_agui.py``). ``echo_runner`` is only
        # a smoke-test stand-in; real traffic needs an injected
        # runner.
        self._agui_router = AGUIRouter(runner=agui_runner or echo_runner)

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        config = uvicorn.Config(
            self._build_app(),
            host=self._host,
            port=self._port,
            log_level="warning",
            lifespan="off",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_server.install_signal_handlers = lambda: None
        self._server_task = asyncio.create_task(
            self._uvicorn_server.serve(), name="modern-mud-server"
        )
        for _ in range(50):
            if self._uvicorn_server.started:
                break
            if self._server_task.done():
                self._server_task.result()
            await asyncio.sleep(0.1)
        if not self._uvicorn_server.started:
            raise RuntimeError(
                f"modern-mud: uvicorn failed to bind {self._host}:{self._port}"
            )
        log.info("modern-mud transport listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._server_task
        self._uvicorn_server = None
        self._server_task = None

    # ------------------------------------------------------------------
    # Inbound / outbound

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self._inbox.get()

    async def send(self, out: OutboundMessage) -> int:
        """Buffer Alice's text for the turn; delivered as one block by
        :meth:`finish_turn`."""
        from ..domain.render import render

        chunks = render(out.text, self.caps)
        if not chunks:
            return 0
        self._pending.setdefault(out.destination.address, []).extend(chunks)
        return len(chunks)

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        return

    async def finish_turn(self, turn_id: str, error: Optional[str] = None) -> None:
        """POST the single response block (or error) to the game."""
        chunks = self._pending.pop(turn_id, [])
        payload: dict = {"turn_id": turn_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["text"] = "\n".join(chunks)
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            KEY_HEADER: self._key,
            SIGNATURE_HEADER: sign(self._key, body),
        }
        try:
            await self._send_callback(self._callback_url, body, headers)
        except Exception:  # noqa: BLE001
            log.exception("modern-mud callback failed for turn %s", turn_id)

    def build_prompt(self, *, principal_name: str, stamp: str, text: str) -> str:
        from prompts import load as load_prompt
        from ..domain.render import capability_prompt_fragment

        return load_prompt(
            "speaking.turn.cli",
            principal_name=principal_name,
            stamp=stamp,
            text=text,
            capability=capability_prompt_fragment("cli", self.caps),
        )

    # ------------------------------------------------------------------
    # Dispatcher integration

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        return asyncio.create_task(self._produce(ctx), name="modern-mud-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        async for msg in self.messages():
            await ctx._queue.put(ModernMudEvent(message=msg))

    async def handle(self, ctx: DaemonContext, event: ModernMudEvent) -> None:
        import datetime

        msg = event.message
        turn_id = msg.origin.address
        prev = (
            ctx._current_turn_kind,
            ctx._current_reply_channel,
            ctx._current_principal_display_name,
        )
        ctx._current_turn_kind = "modern-mud"
        ctx._current_reply_channel = msg.origin
        ctx._current_principal_display_name = msg.principal.display_name
        ctx._current_turn_replied = False
        error: Optional[str] = None
        try:
            stamp = datetime.datetime.now().astimezone().strftime(
                "%A, %B %-d, %Y at %-I:%M %p %Z"
            )
            prompt = self.build_prompt(
                principal_name=msg.principal.display_name,
                stamp=stamp,
                text=msg.text,
            )
            await ctx._run_turn(
                prompt, turn_id=turn_id, outbound_recipient=f"modern-mud:{turn_id}"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("modern-mud turn failed")
            error = f"{type(exc).__name__}: {exc}"
        finally:
            ctx._current_turn_kind, ctx._current_reply_channel, ctx._current_principal_display_name = prev
            await self.finish_turn(turn_id, error)

    # ------------------------------------------------------------------
    # HTTP

    def _build_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/api/modern-mud/turn", self._http_turn, methods=["POST"]),
                Route("/alice/run", self._http_alice_run, methods=["POST"]),
                Route(
                    "/alice/tool-result",
                    self._http_alice_tool_result,
                    methods=["POST"],
                ),
            ]
        )

    def _authorized(self, request: Request) -> bool:
        supplied = request.headers.get(KEY_HEADER, "")
        return bool(self._key) and hmac.compare_digest(supplied, self._key)

    async def _http_turn(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "invalid key"}, status_code=401)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "expected JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "expected JSON object"}, status_code=400)
        user_id = payload.get("user_id")
        message = payload.get("message")
        if not isinstance(user_id, str) or not user_id:
            return JSONResponse({"error": "user_id required"}, status_code=400)
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"error": "message required"}, status_code=400)
        if user_id not in self._admin_user_ids:
            return JSONResponse({"error": "forbidden"}, status_code=403)

        turn_id = uuid.uuid4().hex[:12]
        inbound = InboundMessage(
            principal=Principal(
                transport="modern-mud",
                native_id=self._principal_name,
                display_name=self._principal_display_name,
            ),
            origin=ChannelRef(transport="modern-mud", address=turn_id, durable=False),
            text=message,
            timestamp=time.time(),
            metadata={
                "modern_mud_user_id": user_id,
                "modern_mud_session_id": str(payload.get("session_id") or ""),
            },
        )
        try:
            self._inbox.put_nowait(inbound)
        except asyncio.QueueFull:
            return JSONResponse({"error": "queue full"}, status_code=503)
        return JSONResponse({"turn_id": turn_id}, status_code=202)

    # ------------------------------------------------------------------
    # /alice/run + /alice/tool-result — AG-UI companion endpoints.
    #
    # Same shared-key gate as /api/modern-mud/turn — the private host
    # boundary is the first layer, HMAC on the companion POSTs is the
    # second. Admin-gating of ``user_id`` happens on the game-server
    # side (modern-mud's /Alice command handler already checks it)
    # before any of these endpoints is ever called; keeping that gate
    # off the daemon lets non-admin tests exercise the flow.

    async def _http_alice_run(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "invalid key"}, status_code=401)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "expected JSON body"}, status_code=400)
        try:
            input = parse_run_agent_input(payload)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        headers = {
            # Starlette sets Content-Type for us from ``media_type``.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            self._agui_router.iter_sse(input),
            media_type="text/event-stream",
            headers=headers,
        )

    async def _http_alice_tool_result(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "invalid key"}, status_code=401)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "expected JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "expected JSON object"}, status_code=400
            )
        thread_id = payload.get("threadId")
        run_id = payload.get("runId")
        tool_call_id = payload.get("toolCallId")
        if not isinstance(thread_id, str) or not thread_id:
            return JSONResponse({"error": "threadId required"}, status_code=400)
        if not isinstance(run_id, str) or not run_id:
            return JSONResponse({"error": "runId required"}, status_code=400)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return JSONResponse(
                {"error": "toolCallId required"}, status_code=400
            )
        content = payload.get("content", "")
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            error = str(error)
        delivered = self._agui_router.resolve_tool_call(
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            content=content,
            error=error,
        )
        if not delivered:
            return JSONResponse(
                {"ok": False, "reason": "no pending tool call"},
                status_code=404,
            )
        return JSONResponse({"ok": True})


__all__ = [
    "ModernMudEvent",
    "ModernMudTransport",
    "MODERN_MUD_CAPS",
    "KEY_HEADER",
    "SIGNATURE_HEADER",
    "load_key",
    "sign",
]
