"""Viewer-side glue for the speaking daemon's viewer-chat HTTP ingress.

The daemon (running in the worker container) exposes
``http://<host>:<port>/api/viewer-chat/...`` on 127.0.0.1 by default.
The viewer (running in its own container, sharing the host network
namespace via compose) reaches the daemon at the same host/port. This
module is the thin proxy layer between the viewer's own HTTP routes
and the daemon's transport.

Why a separate module: the viewer's :mod:`viewer.main` is already
a large blob of route handlers. Keeping the proxy isolated mirrors the
``context_probe_client`` split and makes the route handlers in
``main.py`` trivial to read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx


log = logging.getLogger(__name__)

# Defaults match the speaking daemon's ``ViewerChatTransport`` defaults.
# Override via env when the daemon runs on a non-loopback host
# (containerized split deploy).
DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 8181
DEFAULT_TIMEOUT = 10.0


def _base_url() -> str:
    host = os.environ.get("ALICE_VIEWER_CHAT_DAEMON_HOST", DEFAULT_DAEMON_HOST)
    port = int(
        os.environ.get("ALICE_VIEWER_CHAT_DAEMON_PORT", str(DEFAULT_DAEMON_PORT))
    )
    return f"http://{host}:{port}"


async def send_message(
    text: str,
    *,
    channel: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST one inbound message to the daemon and return the JSON ack.

    Raises:
        RuntimeError: the daemon returned a non-2xx status or rejected
            the body. The exception message includes the HTTP status
            and the daemon's error string when available.
        httpx.HTTPError: connection / timeout failure.
    """
    payload: dict[str, Any] = {"text": text}
    if channel:
        payload["channel"] = channel
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{_base_url()}/api/viewer-chat/send", json=payload
        )
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error") or resp.text
        except (ValueError, json.JSONDecodeError):
            err = resp.text
        raise RuntimeError(f"daemon rejected send ({resp.status_code}): {err}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"daemon returned non-JSON ack: {resp.text}") from exc


async def fetch_history(
    *,
    channel: Optional[str] = None,
    limit: int = 100,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch the daemon's in-memory conversation history for ``channel``."""
    params: dict[str, Any] = {"limit": limit}
    if channel:
        params["channel"] = channel
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{_base_url()}/api/viewer-chat/history", params=params
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"daemon history fetch failed ({resp.status_code}): {resp.text}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError("daemon returned non-JSON history") from exc


async def stream_events(
    *,
    channel: Optional[str] = None,
    timeout: float = 600.0,
) -> AsyncIterator[dict[str, Any]]:
    """Open the daemon's SSE stream and yield decoded events.

    Each yielded value is the JSON-decoded payload of one
    ``data: ...`` line. Heartbeat comment lines are silently swallowed.
    The caller is expected to be inside an async generator that
    propagates events to its own SSE client.
    """
    params: dict[str, Any] = {}
    if channel:
        params["channel"] = channel
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "GET",
            f"{_base_url()}/api/viewer-chat/stream",
            params=params,
        ) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"daemon stream failed ({resp.status_code}): "
                    f"{await resp.aread()!r}"
                )
            async for raw in resp.aiter_lines():
                if not raw:
                    continue
                if raw.startswith(":"):
                    # Comment line — heartbeat. Skip.
                    continue
                if not raw.startswith("data:"):
                    continue
                body = raw[len("data:") :].strip()
                if not body:
                    continue
                try:
                    yield json.loads(body)
                except json.JSONDecodeError:
                    log.debug("skipping non-JSON SSE payload: %r", body)
                    continue


async def health(timeout: float = 3.0) -> Optional[dict[str, Any]]:
    """Ping the daemon's health endpoint. Returns the JSON body on
    success or ``None`` when the daemon is unreachable. Used by route
    handlers that want to render a "viewer-chat unavailable" banner
    instead of a hard 502."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{_base_url()}/api/viewer-chat/health")
        if resp.status_code >= 400:
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
        return None


__all__ = [
    "send_message",
    "fetch_history",
    "stream_events",
    "health",
]
