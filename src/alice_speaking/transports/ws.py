"""WSTransport: WebSocket gateway in front of the CLI session protocol.

Lets off-host clients (the iOS app, browser tools, custom desktops)
talk to Alice the same way the in-container CLI does — same NDJSON
event vocabulary, same session model — over a plain TCP WebSocket
instead of an AF_UNIX socket on the container's filesystem.

Why a separate transport (not "remote CLI socket")
==================================================

The CLI socket is bound by uid via ``SO_PEERCRED`` and lives on the
worker's local FS. Neither of those work off-host:

- Unix-socket identity stops at the kernel — you can't ``SO_PEERCRED``
  across a network.
- ``bin/alice`` reaches the socket via ``docker exec``, which is fine
  for shell work but not a real off-host client primitive.

So this transport mirrors :class:`CLITransport`'s wire vocabulary but
swaps the AF_UNIX socket for a token-authenticated WebSocket. Each
accepted connection is its own ephemeral channel — same registry
shape, same session-per-connection semantics. The dispatch path goes
through :func:`alice_speaking._dispatch.handle_ws` (a thin parallel of
``handle_cli`` /``handle_viewer_chat``).

Wire protocol (text frames; one JSON object per frame):

  client → server:
    {"type": "message", "text": "..."}
    {"type": "context", "include_text": true}   -- read-only RPC; mirror of cli

  server → client:
    {"type": "ack"}                              -- received, processing
    {"type": "chunk", "text": "..."}             -- one rendered chunk
    {"type": "tool_use", "name": "..."}          -- (optional) trace event
    {"type": "context_snapshot", "data": {...}}  -- live context composition
    {"type": "done"}                             -- turn ended; reply complete
    {"type": "error", "message": "..."}          -- something went wrong

Auth
====

Operator sets a shared bearer secret via env (default
``ALICE_WS_GATEWAY_TOKEN``). Each connection must present
``Authorization: Bearer <token>``. Missing/bad token → the upgrade is
rejected with HTTP 401 (the WebSocket library raises a 401 response
*before* completing the handshake), so the policy violation never
reaches the session-allocation path. No token in the env → the
transport refuses to bind at all; the gateway is opt-in.

Transport-level TLS is intentionally out of scope. Front the listener
with a reverse proxy / Cloudflare Tunnel / Tailscale Funnel /
whatever's appropriate for the deploy; the gateway speaks plain
``ws://`` and trusts what's in front of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import http
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from .base import (
    CLI_CAPS,
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)


# Default principal id + display name for traffic coming over the
# gateway. v1 is single-user — operator owns the bearer secret, so
# every accepted connection maps to one configured principal. Per-user
# routing (multiple tokens, per-token principals) is a follow-up; the
# shape here leaves room for it (constructor knobs already exist).
DEFAULT_PRINCIPAL_NAME = "jason"
DEFAULT_PRINCIPAL_DISPLAY_NAME = "Jason"

# Canonical WS path. The issue spec ("/cli") and the iOS client agree
# on this — keep it stable.
DEFAULT_WS_PATH = "/cli"


@dataclass
class WSEvent:
    """An inbound WS message wrapped for the dispatcher.

    Mirrors :class:`CLIEvent` / :class:`ViewerChatEvent`: a thin
    envelope around an :class:`InboundMessage` so the dispatcher can
    route by event type without an isinstance ladder.
    """

    message: InboundMessage


class WSTransport:
    """WebSocket gateway. One bearer-authenticated TCP listener; per
    accepted connection one ephemeral CLI-style session.

    Construction does not bind the socket; call :meth:`start`. The
    class deliberately does NOT subclass :class:`CLITransport` — they
    share an event vocabulary on the wire, not internals. The CLI
    transport routes by uid via ``SO_PEERCRED`` and a per-uid
    connection map; this transport routes by the per-connection id
    minted at accept time. Mixing the two would force the CLI to grow
    a token-auth code path it doesn't need.
    """

    name = "ws"
    caps: Capabilities = CLI_CAPS
    event_type = WSEvent

    def __init__(
        self,
        *,
        port: int,
        token: str,
        host: str = "0.0.0.0",
        path: str = DEFAULT_WS_PATH,
        principal_name: str = DEFAULT_PRINCIPAL_NAME,
        principal_display_name: str = DEFAULT_PRINCIPAL_DISPLAY_NAME,
        context_probe: Optional[object] = None,
        inbox_size: int = 64,
    ) -> None:
        if not token:
            # Catch the operator who tries to construct the transport
            # without a shared secret. The daemon's gate already
            # checks this before constructing us — this is a
            # belt-and-braces guard for direct-test callers.
            raise ValueError(
                "WSTransport requires a non-empty bearer token; "
                "leaving the gateway unauthenticated is not supported."
            )
        self._host = host
        self._port = port
        self._token = token
        self._path = path
        self._principal_name = principal_name
        self._principal_display_name = principal_display_name
        # Same hook the CLI transport exposes for ``{"type":"context"}``.
        # The daemon assigns this post-construction so the probe (which
        # depends on TurnRunner) can be wired after the transport exists.
        self.context_probe = context_probe
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        # conn_id → ServerConnection so :meth:`send` can find the right
        # client. Each ephemeral session lives only while the underlying
        # WS connection is open; no per-uid fan-out (the CLI transport
        # does that, but the gateway intentionally keeps one principal
        # per token, so a stranger can't reach an open session by
        # learning a username).
        self._connections: dict[str, "object"] = {}
        # Server task + asyncio Server handle (returned by websockets.serve()).
        self._server: Optional[object] = None
        # Bound port (resolved after start() so tests can ask for the
        # ephemeral port the OS picked when port=0).
        self._bound_port: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        # Import inside start() so the daemon doesn't pay the import
        # cost when the gateway is disabled (no token).
        from websockets.asyncio.server import serve

        async def _process_request(connection, request):
            return self._authorize_request(request)

        self._server = await serve(
            self._handle_connection,
            host=self._host,
            port=self._port,
            process_request=_process_request,
            # Mirror CLI semantics: each frame is one event. Keep frame
            # cap generous (matches CLI_CAPS); reject anything pathological.
            max_size=1_048_576,
        )
        # Resolve the actual bound port. websockets.serve() returns a
        # ``Server`` wrapping an ``asyncio.Server``; the underlying
        # sockets carry the bound address. Useful when port=0 (tests).
        try:
            sockets = getattr(self._server, "sockets", None)
            if not sockets:
                inner = getattr(self._server, "server", None)
                sockets = getattr(inner, "sockets", None)
            if sockets:
                self._bound_port = sockets[0].getsockname()[1]
        except (OSError, AttributeError):
            self._bound_port = self._port
        log.info(
            "WS gateway listening on %s:%d (path %s)",
            self._host,
            self._bound_port or self._port,
            self._path,
        )

    async def stop(self) -> None:
        if self._server is not None:
            close = getattr(self._server, "close", None)
            if close is not None:
                close()
            wait_closed = getattr(self._server, "wait_closed", None)
            if wait_closed is not None:
                with contextlib.suppress(Exception):
                    await wait_closed()
            self._server = None
        # Drain any still-open connections so reconnects on a fresh
        # daemon don't collide with stale state. The websockets server
        # will already have terminated handshakes via ``close()``; this
        # closes lingering sessions that survived (server bug, client
        # misbehaviour).
        for conn in list(self._connections.values()):
            with contextlib.suppress(Exception):
                await conn.close(code=1001, reason="server shutdown")
        self._connections.clear()

    # ------------------------------------------------------------------
    # Inbound stream

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self._inbox.get()

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> int:
        """Deliver Alice's rendered text as one ``chunk`` frame per
        rendered chunk.

        ``out.destination.address`` is a per-connection ``conn_id``
        minted at accept time. The gateway is single-user in v1, so we
        do NOT fan out across all live connections for one principal —
        that would mix turn outputs between two tabs that authenticated
        with the same token. Each turn replies on its inbound channel.
        """
        from ..domain.render import render

        conn = self._connections.get(out.destination.address)
        if conn is None:
            log.warning(
                "ws send: no live connection for address %s; dropping %d chars",
                out.destination.address,
                len(out.text),
            )
            return 0
        chunks = render(out.text, self.caps)
        if not chunks:
            return 0
        delivered = 0
        for chunk in chunks:
            if await self._send_event(conn, {"type": "chunk", "text": chunk}):
                delivered += 1
        return delivered

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """No-op: WS clients render their own typing UI off the lifecycle
        event stream (``turn_start`` → ``text_start`` → …)."""
        return

    # ------------------------------------------------------------------
    # Prompt assembly — reuses the CLI template + capability fragment.
    # Same shape as ViewerChatTransport: the interactive-terminal framing
    # is the right model for a remote client too (full markdown, user
    # waiting at the keyboard, no proactive prelude).

    def build_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
    ) -> str:
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
    # Dispatcher integration (Phase 2 of plan 01)

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Pump :class:`InboundMessage` objects from the WS inbox onto
        ``ctx._queue`` as :class:`WSEvent` events."""
        return asyncio.create_task(self._produce(ctx), name="ws-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        async for msg in self.messages():
            await ctx._queue.put(WSEvent(message=msg))

    async def handle(self, ctx: DaemonContext, event: WSEvent) -> None:
        """Run one turn for one WS event."""
        from .._dispatch import handle_ws

        await handle_ws(ctx, event)

    # ------------------------------------------------------------------
    # Sentinels (called from _dispatch.handle_ws at end-of-turn)

    async def signal_done(self, channel: ChannelRef) -> None:
        conn = self._connections.get(channel.address)
        if conn is None:
            return
        await self._send_event(conn, {"type": "done"})

    async def signal_error(self, channel: ChannelRef, message: str) -> None:
        conn = self._connections.get(channel.address)
        if conn is None:
            return
        await self._send_event(conn, {"type": "error", "message": message})

    async def push_trace(self, channel: ChannelRef, event: dict) -> None:
        """Forward an arbitrary event payload (e.g. tool_use) to a
        connected WS client. No-op if the connection has gone away."""
        conn = self._connections.get(channel.address)
        if conn is None:
            return
        await self._send_event(conn, event)

    async def push_lifecycle_event(self, channel: ChannelRef, event: dict) -> None:
        """Forward a turn-lifecycle event to a connected WS client.

        Same wire path as :meth:`push_trace` — single JSON object per
        text frame. ``CLI_CAPS.lifecycle_events`` is True so the
        speaking daemon's :class:`TurnLifecycleHandler` fires these.
        """
        conn = self._connections.get(channel.address)
        if conn is None:
            return
        await self._send_event(conn, event)

    # ------------------------------------------------------------------
    # Internals

    def _authorize_request(self, request) -> Optional[object]:
        """Validate path + bearer token BEFORE the upgrade completes.

        Returns ``None`` on success (the websockets library proceeds
        with the handshake) or a 4xx :class:`~websockets.http11.Response`
        to reject. We never call ``response.respond`` ourselves — the
        library does that for us once we return a non-None.
        """
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        def _reject(status: http.HTTPStatus, message: str) -> object:
            headers = Headers(
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(message))),
                ]
            )
            return Response(
                status_code=int(status),
                reason_phrase=status.phrase,
                headers=headers,
                body=message.encode("utf-8"),
            )

        # Strip optional query string before path-matching. Reverse
        # proxies + libraries sometimes append "?" + nothing.
        request_path = (getattr(request, "path", "") or "/").split("?", 1)[0]
        if request_path != self._path:
            log.info("ws reject: path %r != %r", request_path, self._path)
            return _reject(http.HTTPStatus.NOT_FOUND, "not found\n")

        auth = request.headers.get("Authorization", "")
        if not auth or not auth.lower().startswith("bearer "):
            log.info("ws reject: missing or non-bearer Authorization header")
            return _reject(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        provided = auth.split(None, 1)[1].strip()
        if not _constant_time_eq(provided, self._token):
            log.info("ws reject: bearer token mismatch")
            return _reject(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def _handle_connection(self, conn) -> None:
        """One accepted WebSocket → one ephemeral session.

        The auth check has already passed by the time we get here
        (failed connections are rejected in :meth:`_authorize_request`
        before the handshake completes). We allocate a fresh conn_id,
        wire the principal/channel, and pump frames until the client
        closes or sends EOF.
        """
        conn_id = uuid.uuid4().hex[:12]
        self._connections[conn_id] = conn
        principal = Principal(
            transport="ws",
            native_id=self._principal_name,
            display_name=self._principal_display_name,
        )
        channel = ChannelRef(transport="ws", address=conn_id, durable=False)
        log.info("ws connection accepted: conn_id=%s", conn_id)

        try:
            async for raw in conn:
                # websockets gives us str for text frames, bytes for binary.
                if isinstance(raw, bytes):
                    text = raw.decode("utf-8", errors="replace")
                else:
                    text = raw
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    await self._send_event(
                        conn,
                        {"type": "error", "message": f"bad json: {exc}"},
                    )
                    continue
                if not isinstance(payload, dict):
                    await self._send_event(
                        conn,
                        {"type": "error", "message": "expected json object"},
                    )
                    continue

                ptype = payload.get("type")
                if ptype == "context":
                    await self._handle_context_request(conn, payload)
                    continue
                if ptype != "message":
                    await self._send_event(
                        conn,
                        {
                            "type": "error",
                            "message": f"unknown event type: {ptype!r}",
                        },
                    )
                    continue

                msg_text = payload.get("text") or ""
                if not isinstance(msg_text, str) or not msg_text.strip():
                    await self._send_event(
                        conn,
                        {
                            "type": "error",
                            "message": "message.text must be a non-empty string",
                        },
                    )
                    continue

                inbound = InboundMessage(
                    principal=principal,
                    origin=channel,
                    text=msg_text,
                    timestamp=time.time(),
                )
                await self._send_event(conn, {"type": "ack"})
                try:
                    self._inbox.put_nowait(inbound)
                except asyncio.QueueFull:
                    await self._send_event(
                        conn,
                        {
                            "type": "error",
                            "message": "alice's queue is full; try again later",
                        },
                    )
        except Exception:  # noqa: BLE001
            # ConnectionClosed / OSError / cancelled — all expected on
            # client disconnect. Log only at debug; the finally block
            # cleans up the connection registry the same way regardless.
            log.debug("ws connection loop ended", exc_info=True)
        finally:
            self._connections.pop(conn_id, None)
            with contextlib.suppress(Exception):
                await conn.close()
            log.info("ws connection closed: conn_id=%s", conn_id)

    async def _handle_context_request(self, conn, payload: dict) -> None:
        """Mirror of :meth:`CLITransport._handle_context_request`.

        Sequence: ``ack`` → ``context_snapshot`` → ``done``. Lets a WS
        client reuse the same ``drain_one_turn`` logic the in-container
        CLI client uses.
        """
        await self._send_event(conn, {"type": "ack"})
        if self.context_probe is None:
            await self._send_event(
                conn,
                {"type": "error", "message": "context probe unavailable"},
            )
            await self._send_event(conn, {"type": "done"})
            return
        include_text = bool(payload.get("include_text", True))
        try:
            snap = self.context_probe.snapshot(include_text=include_text)
            data = snap.to_dict()
        except Exception as exc:  # noqa: BLE001
            log.exception("context probe snapshot failed")
            await self._send_event(
                conn,
                {
                    "type": "error",
                    "message": f"snapshot failed: {type(exc).__name__}: {exc}",
                },
            )
            await self._send_event(conn, {"type": "done"})
            return
        await self._send_event(
            conn, {"type": "context_snapshot", "data": data}
        )
        await self._send_event(conn, {"type": "done"})

    async def _send_event(self, conn, event: dict) -> bool:
        """Encode ``event`` as a single WS text frame. Returns True on
        success, False if the connection already closed. Never raises —
        callers shouldn't have to guard every event emission."""
        try:
            await conn.send(json.dumps(event))
            return True
        except Exception:  # noqa: BLE001
            log.debug("ws send failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Test helpers — internal, no external callers should rely on these.

    @property
    def bound_port(self) -> Optional[int]:
        """Port the listener actually bound. ``None`` until :meth:`start`
        runs. Tests pass ``port=0`` and read this to get the ephemeral
        port the kernel handed out."""
        return self._bound_port


def _constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings without leaking length-difference timing.

    ``hmac.compare_digest`` does the real work; the wrapper accepts
    plain str so callers don't have to encode at every site.
    """
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ----------------------------------------------------------------------
# Token-env helper. The daemon uses this to decide whether to construct
# the transport at startup; the helper lives next to the transport so
# the gating logic and the value resolution stay in one file.


def resolve_token(env_var_name: str = "ALICE_WS_GATEWAY_TOKEN") -> str:
    """Return the shared bearer secret from the env, or empty string
    when unset. Empty string is the "gateway disabled" sentinel.

    A separate function (not just ``os.environ.get`` inline at the
    call site) so the daemon and tests can stub the lookup
    consistently — and so the choice of env-var name is centralized.
    """
    return (os.environ.get(env_var_name) or "").strip()


__all__ = [
    "WSEvent",
    "WSTransport",
    "DEFAULT_PRINCIPAL_NAME",
    "DEFAULT_PRINCIPAL_DISPLAY_NAME",
    "DEFAULT_WS_PATH",
    "resolve_token",
]
