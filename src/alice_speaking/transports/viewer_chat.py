"""ViewerChatTransport: web-chat bridge for the local viewer UI.

Lets the operator hold a conversation with Alice inside the same browser
tab they're using to read the viewer. Lives on a small HTTP ingress at
``127.0.0.1:8181`` (configurable) so the viewer (which runs in its own
container with no AF_UNIX access to the worker) can POST inbound text
and stream outbound chunks via SSE without copying signal-cli scaffolding.

Wire shape
==========

Inbound (viewer → daemon):

  ``POST /api/viewer-chat/send``
      Body: ``{"text": "...", "channel": "viewer-chat-main"}``
      ``channel`` is optional; defaults to ``viewer-chat-main`` so v1
      callers don't have to think about it. The field is the seam for
      multi-tab / multi-conversation support — a future viewer can mint
      per-tab channel ids and the transport will route replies back to
      the right SSE subscriber.

Outbound (daemon → viewer):

  ``GET /api/viewer-chat/stream?channel=viewer-chat-main`` (SSE)
      Emits JSON lines under SSE ``data:`` events. Event shapes mirror
      the CLI socket's wire protocol so the viewer JS doesn't have to
      learn a second vocabulary:

        {"type": "ack"}             — receipt acknowledgment
        {"type": "chunk", "text"}   — one rendered outbound chunk
        {"type": "done"}            — turn finished
        {"type": "error", "message"} — turn failed

Identity
========

All viewer-chat traffic maps to a single configured principal (default
``jason``). The principal's display name is taken from the address book
when it has a viewer-chat channel entry; otherwise we fall back to a
literal ``jason`` / ``Jason`` rendering. Listening only on 127.0.0.1
means we trust the host boundary the same way the CLI socket does — no
auth on the wire.

Why not a Unix socket
=====================

The viewer and worker run in separate containers. The CLI socket
already proved that AF_UNIX over a bind-mount from macOS / Rancher
Desktop hits virtiofs EPERM. HTTP loopback on 127.0.0.1 is the
lowest-friction transport that crosses the container boundary on every
host we run on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .base import (
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)


# Default channel id for the v1 single-conversation viewer chat. The
# transport accepts arbitrary channel strings on inbound (multi-channel
# hook), but the viewer UI ships with one main conversation today.
DEFAULT_CHANNEL_ID = "viewer-chat-main"

# Default identity: viewer-chat is Jason's web channel. Override via
# constructor for tests / alt minds.
DEFAULT_PRINCIPAL_NAME = "jason"
DEFAULT_PRINCIPAL_DISPLAY_NAME = "Jason"


# Capability profile. Viewer renders markdown via marked.js + DOMPurify
# (already loaded in base.html) so we ship full markdown unstripped, the
# same shape Alice writes in. Chunk limit is generous: SSE has no
# per-event size cap that matters here, and the JS reassembles chunks
# naturally on the receiving end.
VIEWER_CHAT_CAPS = Capabilities(
    markdown="full",
    code_blocks=True,
    images_outbound=False,
    files_outbound=False,
    max_message_bytes=200_000,
    long_message_strategy="split",
    typing_indicator=False,
    reactions=False,
    interactive=True,
)


@dataclass
class ViewerChatEvent:
    """An inbound viewer-chat message wrapped for the dispatcher.

    Mirrors :class:`CLIEvent` / :class:`DiscordEvent`: a thin envelope
    around an :class:`InboundMessage` so the dispatcher can route by
    event type. Re-exported from ``alice_speaking.daemon`` for
    back-compat alongside the other transport events.
    """

    message: InboundMessage


class ViewerChatTransport:
    """HTTP-loopback transport for the viewer's chat panel.

    Lifecycle: ``start()`` brings up a uvicorn server in a background
    task on ``host:port``; ``stop()`` flips the should-exit flag and
    awaits the task. Per-channel outbound queues feed SSE subscribers;
    when no subscriber is live for a channel, sends are buffered up to
    ``outbox_buffer_max`` so a momentary reconnect doesn't lose chunks.

    Multi-channel hook: every inbound carries a ``channel`` field
    (defaulting to ``viewer-chat-main``). The transport tracks one
    outbound queue per channel id, so when v2 wants per-tab
    conversations it just needs to vary the channel id on the wire —
    no transport-side changes required.
    """

    name = "viewer-chat"
    caps: Capabilities = VIEWER_CHAT_CAPS
    event_type = ViewerChatEvent

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8181,
        principal_name: str = DEFAULT_PRINCIPAL_NAME,
        principal_display_name: str = DEFAULT_PRINCIPAL_DISPLAY_NAME,
        default_channel_id: str = DEFAULT_CHANNEL_ID,
        inbox_size: int = 64,
        outbox_buffer_max: int = 256,
    ) -> None:
        self._host = host
        self._port = port
        self._principal_name = principal_name
        self._principal_display_name = principal_display_name
        self._default_channel_id = default_channel_id
        self._outbox_buffer_max = outbox_buffer_max
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        # channel_id → per-subscriber outbound queues. Multiple SSE
        # subscribers on the same channel (operator with two tabs open)
        # each get their own queue so neither steals chunks from the
        # other. A weak fan-out, but exactly what the viewer needs.
        self._subscribers: dict[str, list[asyncio.Queue[dict]]] = {}
        # Per-channel buffered history of events while no subscriber is
        # attached. On (re)connect we replay the buffer so a brief
        # disconnection doesn't black-hole an in-flight turn. Capped per
        # channel; older events are dropped.
        self._buffer: dict[str, list[dict]] = {}
        # Append-only history: every text exchange the transport has
        # carried in this process. Indexed by channel. The viewer's
        # ``/api/chat/history`` route reads this to seed the UI on
        # first load. Capped at ``history_max`` per channel.
        self._history: dict[str, list[dict]] = {}
        self._history_max = 200
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        """Bind the HTTP listener. Returns once uvicorn is accepting
        connections so the daemon's ``daemon_ready`` event can fire only
        after the ingress is actually live (same pattern as A2A)."""
        app = self._build_app()
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            # Mirror A2A: avoid uvicorn stomping on the daemon's signal
            # handlers, and skip the lifespan protocol since we don't
            # define startup/shutdown handlers on the app.
            lifespan="off",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_server.install_signal_handlers = lambda: None
        self._server_task = asyncio.create_task(
            self._uvicorn_server.serve(), name="viewer-chat-server"
        )
        for _ in range(50):
            if self._uvicorn_server.started:
                break
            if self._server_task.done():
                self._server_task.result()
            await asyncio.sleep(0.1)
        if not self._uvicorn_server.started:
            raise RuntimeError(
                f"viewer-chat: uvicorn failed to bind {self._host}:{self._port} "
                f"within 5s"
            )
        log.info(
            "viewer-chat transport listening on http://%s:%d",
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._server_task
        self._uvicorn_server = None
        self._server_task = None
        # Drain SSE subscribers so any pending reads unblock and we
        # don't leak per-channel queues across daemon restarts.
        for subs in self._subscribers.values():
            for q in subs:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait({"type": "done"})
        self._subscribers.clear()

    # ------------------------------------------------------------------
    # Inbound

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self._inbox.get()

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> int:
        """Render Alice's text per :data:`VIEWER_CHAT_CAPS` and push each
        chunk into the per-channel outbox + history.

        ``out.destination.address`` is the channel id (``viewer-chat-main``
        in v1). Chunks fan out to every live SSE subscriber on the
        channel; when there's no subscriber, the chunks land in the
        per-channel buffer for replay on the next connect. Returns the
        chunk count delivered.
        """
        from ..domain.render import render

        chunks = render(out.text, self.caps)
        if not chunks:
            return 0
        channel_id = out.destination.address or self._default_channel_id
        # Always update history so /api/chat/history can render the
        # conversation regardless of subscriber state.
        joined = "".join(chunks)
        self._append_history(channel_id, role="alice", text=joined)
        for chunk in chunks:
            self._broadcast(channel_id, {"type": "chunk", "text": chunk})
        return len(chunks)

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """No-op for viewer-chat. The UI can render its own typing
        indicator off the ``ack`` event sequence if it wants one."""
        return

    # ------------------------------------------------------------------
    # Daemon-facing sentinels (called from _dispatch.handle_viewer_chat)

    async def signal_done(self, channel: ChannelRef) -> None:
        """Tell the client a turn finished. Mirrors CLITransport."""
        self._broadcast(channel.address, {"type": "done"})

    async def signal_error(self, channel: ChannelRef, message: str) -> None:
        self._broadcast(channel.address, {"type": "error", "message": message})

    # ------------------------------------------------------------------
    # Prompt assembly — reuses CLI templates. Phase 6c of plan 01 puts
    # transport-specific prompt bodies under
    # ``alice_prompts/templates/speaking/turn.<name>.md.j2``. Viewer
    # chat reuses the CLI prompt + CLI capability fragment because the
    # interactive-terminal framing is the right model for it too (full
    # markdown, user waiting at the keyboard, no proactive prelude).

    def build_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
    ) -> str:
        from alice_prompts import load as load_prompt
        from ..domain.render import capability_prompt_fragment

        return load_prompt(
            "speaking.turn.cli",
            principal_name=principal_name,
            stamp=stamp,
            text=text,
            capability=capability_prompt_fragment("cli", self.caps),
        )

    # ------------------------------------------------------------------
    # Dispatcher integration (Phase 2 of plan 01)

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Pump :class:`InboundMessage` objects from the HTTP ingress
        onto ``ctx._queue`` as :class:`ViewerChatEvent` events."""
        return asyncio.create_task(self._produce(ctx), name="viewer-chat-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        async for msg in self.messages():
            await ctx._queue.put(ViewerChatEvent(message=msg))

    async def handle(self, ctx: DaemonContext, event: ViewerChatEvent) -> None:
        """Run one turn for one viewer-chat event."""
        from .._dispatch import handle_viewer_chat

        await handle_viewer_chat(ctx, event)

    # ------------------------------------------------------------------
    # History (read by the viewer-side /api/chat/history route)

    def history_for(
        self, channel_id: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        """Return the recent conversation log for ``channel_id``.

        Each entry is ``{"role": "user"|"alice", "text", "ts"}``. The
        list is in arrival order; the viewer reverses for display.
        """
        cid = channel_id or self._default_channel_id
        items = self._history.get(cid, [])
        if limit and limit > 0:
            items = items[-limit:]
        return list(items)

    # ------------------------------------------------------------------
    # HTTP app + routes

    def _build_app(self) -> Starlette:
        routes = [
            Route(
                "/api/viewer-chat/send",
                self._http_send,
                methods=["POST"],
            ),
            Route(
                "/api/viewer-chat/stream",
                self._http_stream,
                methods=["GET"],
            ),
            Route(
                "/api/viewer-chat/history",
                self._http_history,
                methods=["GET"],
            ),
            Route(
                "/api/viewer-chat/health",
                self._http_health,
                methods=["GET"],
            ),
        ]
        return Starlette(routes=routes)

    async def _http_send(self, request: Request) -> Response:
        """Accept one inbound message. Returns 202 on success with the
        channel id the caller should subscribe to for the reply stream.
        """
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "expected JSON body"}, status_code=400
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "expected JSON object"}, status_code=400
            )
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "text must be a non-empty string"}, status_code=400
            )
        channel_id = (payload.get("channel") or self._default_channel_id).strip()
        if not channel_id:
            channel_id = self._default_channel_id

        inbound = self._make_inbound(text=text, channel_id=channel_id)
        self._append_history(channel_id, role="user", text=text)
        # Ack arrives on the SSE stream so the UI can mark the message
        # as accepted. Push into the per-channel outbox BEFORE the
        # inbound goes into the dispatcher so subscribers see the ack
        # ordered correctly relative to subsequent chunks.
        self._broadcast(channel_id, {"type": "ack"})
        try:
            self._inbox.put_nowait(inbound)
        except asyncio.QueueFull:
            self._broadcast(
                channel_id,
                {
                    "type": "error",
                    "message": "alice's queue is full; try again later",
                },
            )
            return JSONResponse(
                {"error": "queue full"}, status_code=503
            )
        return JSONResponse(
            {"ok": True, "channel": channel_id}, status_code=202
        )

    async def _http_history(self, request: Request) -> Response:
        channel_id = request.query_params.get("channel") or self._default_channel_id
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            limit = 100
        return JSONResponse(
            {
                "channel": channel_id,
                "messages": self.history_for(channel_id, limit=limit),
            }
        )

    async def _http_health(self, request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "transport": self.name,
                "default_channel": self._default_channel_id,
            }
        )

    async def _http_stream(self, request: Request) -> Response:
        """SSE stream — emits one ``data:`` line per outbound event.

        Implemented inline (instead of sse-starlette) so the transport
        has no extra dep. The handler stays minimal: subscribe to the
        channel, replay any buffered events, then pump until the
        client disconnects.
        """
        from starlette.responses import StreamingResponse

        channel_id = (
            request.query_params.get("channel") or self._default_channel_id
        )
        q = self._subscribe(channel_id)

        async def event_gen():
            # Replay the buffer first so reconnects don't black-hole
            # an in-flight turn's chunks.
            buffered = self._buffer.pop(channel_id, [])
            for event in buffered:
                yield _format_sse(event)
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Heartbeat keeps intermediate proxies from
                        # idling the connection. SSE comments are the
                        # standard mechanism.
                        yield ": heartbeat\n\n"
                        continue
                    yield _format_sse(event)
            finally:
                self._unsubscribe(channel_id, q)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # Internals

    def _make_inbound(self, *, text: str, channel_id: str) -> InboundMessage:
        principal = Principal(
            transport="viewer-chat",
            native_id=self._principal_name,
            display_name=self._principal_display_name,
        )
        origin = ChannelRef(
            transport="viewer-chat", address=channel_id, durable=True
        )
        return InboundMessage(
            principal=principal,
            origin=origin,
            text=text,
            timestamp=time.time(),
            metadata={"viewer_chat_message_id": uuid.uuid4().hex[:12]},
        )

    def _subscribe(self, channel_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.setdefault(channel_id, []).append(q)
        return q

    def _unsubscribe(self, channel_id: str, q: asyncio.Queue[dict]) -> None:
        subs = self._subscribers.get(channel_id)
        if not subs:
            return
        with contextlib.suppress(ValueError):
            subs.remove(q)
        if not subs:
            self._subscribers.pop(channel_id, None)

    def _broadcast(self, channel_id: str, event: dict) -> None:
        """Fan an event out to every subscriber on the channel, or
        buffer it when there's none. Drops oldest buffered events past
        ``outbox_buffer_max``."""
        subs = self._subscribers.get(channel_id) or []
        if subs:
            for q in subs:
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)
            return
        buf = self._buffer.setdefault(channel_id, [])
        buf.append(event)
        if len(buf) > self._outbox_buffer_max:
            del buf[: len(buf) - self._outbox_buffer_max]

    def _append_history(self, channel_id: str, *, role: str, text: str) -> None:
        h = self._history.setdefault(channel_id, [])
        h.append({"role": role, "text": text, "ts": time.time()})
        if len(h) > self._history_max:
            del h[: len(h) - self._history_max]


def _format_sse(event: dict) -> str:
    """Encode one event as a single SSE message line."""
    return f"data: {json.dumps(event)}\n\n"


__all__ = [
    "ViewerChatEvent",
    "ViewerChatTransport",
    "VIEWER_CHAT_CAPS",
    "DEFAULT_CHANNEL_ID",
]
