"""CLITransport: a Unix-domain-socket transport for local CLI / agent traffic.

Listens on a Unix socket inside the worker container (default
``/state/alice.sock``). Each connection is one ephemeral channel — when
the client disconnects, that ChannelRef is no longer reachable.

Wire protocol (line-delimited JSON, UTF-8):

  client → server:
    {"type": "message", "text": "..."}

  server → client:
    {"type": "ack"}                                -- received, processing
    {"type": "chunk", "text": "..."}               -- one rendered chunk
    {"type": "tool_use", "name": "..."}            -- (optional) trace event
    {"type": "done"}                               -- turn ended; reply complete
    {"type": "error", "message": "..."}            -- something went wrong

The connection stays open across turns; the client decides when to
disconnect. That's how interactive mode works: client loops `read line
→ send message → drain until done → next line`.

Identity: the connecting process's uid (read from ``SO_PEERCRED``) is
the principal's ``native_id``. Multiple simultaneous connections from
the same uid share the same Principal but get distinct ChannelRefs (so
two terminals can hold separate conversations).

ACL (Phase 3+): the daemon supplies an ``is_allowed`` callback that
consults the :class:`AddressBook`. The transport falls back to
"only-the-running-uid" when no callback is supplied — useful for tests
and standalone harnesses where there's no address book.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import socket
import struct
import time
import uuid
from typing import AsyncIterator, Callable, Optional

from .base import (
    CLI_CAPS,
    Capabilities,
    ChannelRef,
    InboundMessage,
    OutboundMessage,
    Principal,
)


# Callback the transport invokes during connection accept to decide
# whether ``str(peer_uid)`` is allowed in. The daemon binds this to
# ``address_book.is_allowed("cli", uid)``; tests can pass a stub.
ACLCallable = Callable[[str], bool]


log = logging.getLogger(__name__)


# Linux SO_PEERCRED: returns (pid, uid, gid) as three int32.
_SCM_CREDS_STRUCT = struct.Struct("3i")


class CLITransport:
    """Unix-socket transport for in-container CLI/agent traffic.

    Construction does not open the socket; call :meth:`start` first.
    The class is intentionally minimal — no auth tokens, no TLS, no
    cross-host. Anyone with ``docker exec`` already has full container
    access; the uid check just prevents random in-container daemons
    from poking the socket.
    """

    name = "cli"
    caps: Capabilities = CLI_CAPS

    def __init__(
        self,
        *,
        socket_path: pathlib.Path,
        is_allowed: Optional[ACLCallable] = None,
        principal_name_for: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._socket_path = socket_path
        # ACL: defer to the address book when wired by the daemon. When no
        # callback is supplied (tests, standalone harnesses) accept the
        # current process's own uid only — a sensible local-dev default.
        if is_allowed is None:
            own_uid = str(os.getuid())
            is_allowed = lambda uid: uid == own_uid  # noqa: E731
        self._is_allowed = is_allowed
        # Optional display-name lookup (address-book backed). Falls back to
        # the legacy ``"local (uid=N)"`` rendering when not supplied.
        self._principal_name_for = principal_name_for
        self._server: Optional[asyncio.AbstractServer] = None
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=64)
        # connection_id → StreamWriter, so :meth:`send` can find the right
        # client to deliver an OutboundMessage to.
        self._writers: dict[str, asyncio.StreamWriter] = {}

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        # Remove a stale socket file if one exists from a previous run.
        # asyncio.start_unix_server doesn't unlink for us.
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self._socket_path)
        )
        # Mode 0600 — only the alice user can connect.
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError as exc:
            log.warning("could not chmod %s: %s", self._socket_path, exc)
        log.info("CLI transport listening at %s", self._socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for writer in list(self._writers.values()):
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
        self._writers.clear()
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    # ------------------------------------------------------------------
    # Inbound stream

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            msg = await self._inbox.get()
            yield msg

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> None:
        """Deliver an OutboundMessage to the correct connection.

        ``out.destination.address`` is the connection_id assigned at
        accept time. If that connection has gone away (client
        disconnected mid-turn), the send is logged and dropped — there's
        no recovery for an ephemeral channel.
        """
        conn_id = out.destination.address
        writer = self._writers.get(conn_id)
        if writer is None:
            log.warning(
                "cli send: connection %s no longer present; dropping %d chars",
                conn_id,
                len(out.text),
            )
            return
        await self._write_event(writer, {"type": "chunk", "text": out.text})

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """No-op for CLI — terminals don't have typing indicators."""
        return

    # ------------------------------------------------------------------
    # Event sentinels for the daemon to write through us

    async def signal_done(self, channel: ChannelRef) -> None:
        """Tell the client a turn finished. Called by the daemon after the
        kernel run completes.
        """
        writer = self._writers.get(channel.address)
        if writer is None:
            return
        await self._write_event(writer, {"type": "done"})

    async def signal_error(self, channel: ChannelRef, message: str) -> None:
        writer = self._writers.get(channel.address)
        if writer is None:
            return
        await self._write_event(writer, {"type": "error", "message": message})

    # ------------------------------------------------------------------
    # Internals

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_uid = _peer_uid(writer)
        peer_uid_str = str(peer_uid) if peer_uid is not None else ""
        if peer_uid is None or not self._is_allowed(peer_uid_str):
            log.info("cli connection rejected: peer_uid=%s", peer_uid)
            with contextlib.suppress(Exception):
                await self._write_event(
                    writer,
                    {
                        "type": "error",
                        "message": (
                            f"unauthorized: uid {peer_uid} is not in the "
                            f"address book"
                        ),
                    },
                )
                writer.close()
                await writer.wait_closed()
            return

        conn_id = uuid.uuid4().hex[:12]
        if self._principal_name_for is not None:
            display_name = self._principal_name_for(peer_uid_str)
        else:
            display_name = f"local (uid={peer_uid})"
        principal = Principal(
            transport="cli",
            native_id=peer_uid_str,
            display_name=display_name,
        )
        channel = ChannelRef(transport="cli", address=conn_id, durable=False)
        self._writers[conn_id] = writer
        log.info(
            "cli connection accepted: conn_id=%s uid=%d",
            conn_id,
            peer_uid,
        )

        try:
            while not reader.at_eof():
                try:
                    line = await reader.readline()
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    await self._write_event(
                        writer,
                        {
                            "type": "error",
                            "message": f"bad json: {exc}",
                        },
                    )
                    continue

                if not isinstance(payload, dict):
                    await self._write_event(
                        writer,
                        {"type": "error", "message": "expected json object"},
                    )
                    continue

                ptype = payload.get("type")
                if ptype != "message":
                    await self._write_event(
                        writer,
                        {
                            "type": "error",
                            "message": f"unknown event type: {ptype!r}",
                        },
                    )
                    continue

                text = payload.get("text") or ""
                if not isinstance(text, str) or not text.strip():
                    await self._write_event(
                        writer,
                        {
                            "type": "error",
                            "message": "message.text must be a non-empty string",
                        },
                    )
                    continue

                inbound = InboundMessage(
                    principal=principal,
                    origin=channel,
                    text=text,
                    timestamp=time.time(),
                )
                await self._write_event(writer, {"type": "ack"})
                try:
                    self._inbox.put_nowait(inbound)
                except asyncio.QueueFull:
                    await self._write_event(
                        writer,
                        {
                            "type": "error",
                            "message": "alice's queue is full; try again later",
                        },
                    )
        finally:
            self._writers.pop(conn_id, None)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            log.info("cli connection closed: conn_id=%s", conn_id)

    async def _write_event(
        self,
        writer: asyncio.StreamWriter,
        event: dict,
    ) -> None:
        try:
            writer.write((json.dumps(event) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            log.debug("cli write failed: %s", exc)


def _peer_uid(writer: asyncio.StreamWriter) -> Optional[int]:
    """Read SO_PEERCRED from the underlying socket and return the uid.

    Linux-only. Returns None if the socket isn't AF_UNIX or the lookup
    fails (which would be a kernel/setup bug — we log and reject).
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        creds = sock.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _SCM_CREDS_STRUCT.size,
        )
    except OSError as exc:
        log.warning("SO_PEERCRED lookup failed: %s", exc)
        return None
    _pid, uid, _gid = _SCM_CREDS_STRUCT.unpack(creds)
    return int(uid)
