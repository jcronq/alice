"""A2AClient: Alice as a Google A2A protocol *client*.

The companion to :mod:`alice_speaking.transports.a2a`. That module is the
*server* side — external agents POST to us and we stream a turn back.
This module is the *client* side — we POST to a peer agent, stream their
reply back into our own turn log.

Together the two give Alice **symmetric** peering: either side can
initiate, either side can reply. Grand-alice pings main-alice; main-alice
pings grand-alice; both resolve the other via a ``principals.yaml``
entry of the form::

    grand-alice:
      display_name: Grand Alice
      channels:
        - transport: a2a
          address: http://10.200.0.2:7878
          preferred: true

Address book resolution yields a ``ChannelRef(transport="a2a",
address="http://...")`` and the outbox router dispatches to the a2a
transport. :meth:`A2ATransport.send` recognises the URL-shaped address
and delegates to :class:`A2AClient` below instead of the per-task server
outbox.

Auth (v1): a single shared-secret header, ``X-Alice-Peer-Token``. Each
Alice is configured with:

- ``identity_token`` — the token we present on outbound (``*both*`` ends
  share the same secret when they peer; one Alice's ``identity_token`` is
  the other Alice's ``inbound token for peer X``).
- ``peer_tokens`` — the token→principal-id map applied to inbound
  requests so the server-side executor resolves callers to the right
  principal (``main-alice`` vs. ``grand-alice``) instead of the catch-all
  ``a2a`` principal.

The client is deliberately thin: one short-lived httpx-backed a2a-sdk
``Client`` per peer URL, cached to reuse the connection pool. Caller
blocks on :meth:`send_text` until either the peer sends a terminal
``TaskState`` (``COMPLETED`` / ``CANCELED`` / ``FAILED``) or the
configurable timeout expires. The streamed artifact text is aggregated
and returned so :func:`send_message_from_args` can include the reply
in the tool result Alice sees.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx
from a2a.client import (
    A2AClientError,
    A2AClientTimeoutError,
    ClientConfig,
    ClientFactory,
    minimal_agent_card,
)
from a2a.helpers import (
    get_artifact_text,
    get_message_text,
    new_text_message,
)
from a2a.types import (
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.constants import TransportProtocol


log = logging.getLogger(__name__)


# Header name presented on every outbound A2A request. Matches the
# header the server-side :class:`A2ATransport` reads from
# ``call_context.state['headers']`` to resolve the caller to a
# principal.
PEER_TOKEN_HEADER = "x-alice-peer-token"


# Default per-request timeout. Overrideable per-call on
# :meth:`A2AClient.send_text`; also overrideable globally via the
# ``ALICE_A2A_CLIENT_TIMEOUT`` env var so operators can widen it on
# slow WireGuard links without a code change.
_DEFAULT_TIMEOUT_S = 60.0


# Terminal task states — stop streaming once we see one of these. The
# a2a-sdk emits status updates through the same StreamResponse as
# artifact chunks, so the client has to inspect every event.
_TERMINAL_STATES: frozenset[TaskState.ValueType] = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_REJECTED,
    }
)


@dataclass
class A2AReply:
    """Aggregated result of one outbound A2A call.

    ``text`` is the concatenation of every text artifact chunk the peer
    streamed back, in order. ``state`` is the terminal task state the
    peer reported. ``timed_out`` is ``True`` if we aborted before seeing
    a terminal state. The tuple is deliberately small so the tool
    result Alice sees stays scannable.
    """

    text: str = ""
    state: Optional[TaskState.ValueType] = None
    timed_out: bool = False
    error: Optional[str] = None
    chunk_count: int = 0
    # Any non-terminal status-update messages the peer emitted while
    # working (e.g. "thinking…"). Logged but not surfaced to Alice —
    # noise unless the turn fails, in which case the last status
    # message can be a useful clue.
    status_messages: list[str] = field(default_factory=list)


def resolve_identity_token() -> Optional[str]:
    """Look up the outbound identity token from the environment.

    Reads ``ALICE_A2A_IDENTITY_TOKEN`` by default. When unset (dev /
    off-mesh runs), outbound calls still go out — the peer just
    maps them to the unauthenticated catch-all principal. Deploys
    behind the WireGuard mesh should always export the token.
    """
    tok = (os.environ.get("ALICE_A2A_IDENTITY_TOKEN") or "").strip()
    return tok or None


def resolve_client_timeout() -> float:
    """Env-overridable per-request timeout (seconds)."""
    raw = (os.environ.get("ALICE_A2A_CLIENT_TIMEOUT") or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "ignoring malformed ALICE_A2A_CLIENT_TIMEOUT=%r; using default %.1fs",
            raw,
            _DEFAULT_TIMEOUT_S,
        )
        return _DEFAULT_TIMEOUT_S
    if value <= 0:
        log.warning(
            "ALICE_A2A_CLIENT_TIMEOUT must be positive; got %.3f, using default",
            value,
        )
        return _DEFAULT_TIMEOUT_S
    return value


class A2AClient:
    """Thin outbound A2A client wrapping ``a2a-sdk``'s JSON-RPC transport.

    One instance per :class:`A2ATransport`; peer-url-keyed
    ``_client_cache`` holds one short-lived SDK client per peer so we
    reuse the underlying httpx connection pool. Clients are rebuilt on
    error — the SDK's :class:`BaseClient` doesn't expose a "is still
    healthy?" check and we'd rather pay a card re-resolution than
    serve a stale socket.
    """

    def __init__(
        self,
        *,
        identity_token: Optional[str] = None,
        default_timeout_s: Optional[float] = None,
    ) -> None:
        self._identity_token = identity_token or resolve_identity_token()
        self._default_timeout_s = (
            default_timeout_s
            if default_timeout_s is not None
            else resolve_client_timeout()
        )
        self._httpx: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._client_cache: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Lifecycle

    async def aclose(self) -> None:
        """Release the underlying httpx client + any cached a2a clients.

        Safe to call twice. The parent :class:`A2ATransport` invokes
        this from :meth:`A2ATransport.stop` so the daemon's shutdown
        path drains outbound sockets cleanly.
        """
        async with self._lock:
            for client in self._client_cache.values():
                close = getattr(client, "close", None)
                if close is None:
                    continue
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    log.debug("a2a client close failed: %s", exc)
            self._client_cache.clear()
            if self._httpx is not None:
                try:
                    await self._httpx.aclose()
                except Exception as exc:  # noqa: BLE001
                    log.debug("a2a httpx close failed: %s", exc)
                self._httpx = None

    # ------------------------------------------------------------------
    # Public send

    async def send_text(
        self,
        *,
        url: str,
        text: str,
        timeout_s: Optional[float] = None,
    ) -> A2AReply:
        """POST a text task to the peer at ``url`` and stream the reply.

        Returns an :class:`A2AReply` carrying the aggregated artifact
        text plus the terminal task state. A timeout or RPC error turns
        into an :class:`A2AReply` with ``timed_out`` / ``error`` set —
        we never raise through this method so the caller (the outbox
        router → :meth:`A2ATransport.send`) can surface the failure to
        the model without blowing up the turn.
        """
        if not url:
            return A2AReply(error="empty peer URL")
        if not text:
            return A2AReply(error="empty message")

        effective_timeout = (
            timeout_s if timeout_s is not None else self._default_timeout_s
        )

        try:
            client = await self._get_or_build_client(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("a2a client factory failed for %s: %s", url, exc)
            return A2AReply(error=f"client_build_failed: {exc}")

        message = new_text_message(text=text, role=Role.ROLE_USER)
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(
                accepted_output_modes=["text/plain"]
            ),
        )

        reply = A2AReply()
        try:
            async with asyncio.timeout(effective_timeout):
                async for response in client.send_message(request):
                    self._ingest_stream_response(response, reply)
                    if reply.state in _TERMINAL_STATES:
                        break
        except asyncio.TimeoutError:
            log.warning(
                "a2a outbound to %s timed out after %.1fs (partial text=%d chars)",
                url,
                effective_timeout,
                len(reply.text),
            )
            reply.timed_out = True
            return reply
        except A2AClientTimeoutError as exc:
            log.warning("a2a client timeout against %s: %s", url, exc)
            reply.timed_out = True
            reply.error = str(exc)
            return reply
        except A2AClientError as exc:
            log.warning("a2a client error against %s: %s", url, exc)
            reply.error = f"a2a_client_error: {exc}"
            # Invalidate the cached client so the next send rebuilds
            # with a fresh card resolution — stale socket / rotated
            # card are the common causes.
            self._client_cache.pop(url, None)
            return reply
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "a2a outbound to %s failed unexpectedly: %s", url, exc
            )
            reply.error = f"{type(exc).__name__}: {exc}"
            self._client_cache.pop(url, None)
            return reply
        return reply

    # ------------------------------------------------------------------
    # Internals

    def _ingest_stream_response(self, response, reply: A2AReply) -> None:
        """Fold one SDK ``StreamResponse`` into ``reply``.

        The SDK multiplexes task metadata, status updates, and artifact
        chunks onto a single stream, so the client has to branch per
        payload type. We only care about:

        - ``artifact_update``: append text to ``reply.text``.
        - ``status_update``: record the state + any message text (the
          latter only kept for the error trail).
        - ``task``: initial Task payload — ignore; the subsequent
          status updates carry the terminal state we actually want.
        - ``message``: unary (non-streaming) reply — treat the whole
          message text as the reply body.
        """
        if response.HasField("artifact_update"):
            text = get_artifact_text(response.artifact_update.artifact)
            if text:
                reply.text += text
                reply.chunk_count += 1
            return
        if response.HasField("status_update"):
            status = response.status_update.status
            reply.state = status.state
            if status.HasField("message"):
                msg_text = get_message_text(status.message)
                if msg_text:
                    reply.status_messages.append(msg_text)
            return
        if response.HasField("message"):
            # Non-streaming reply path: the peer replied with a single
            # Message rather than a Task. Treat it as fully delivered.
            msg_text = get_message_text(response.message)
            if msg_text:
                reply.text += msg_text
                reply.chunk_count += 1
            reply.state = TaskState.TASK_STATE_COMPLETED
            return
        # task / other non-payload frames — ignore silently.

    async def _get_or_build_client(self, url: str):
        """Return a cached SDK client for ``url``, building on miss.

        Guarded by :attr:`_lock` so a burst of concurrent
        :meth:`send_text` calls against a fresh peer only pays the
        card-resolution cost once.
        """
        existing = self._client_cache.get(url)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._client_cache.get(url)
            if existing is not None:
                return existing
            client = await self._build_client(url)
            self._client_cache[url] = client
            return client

    async def _build_client(self, url: str):
        """Construct a fresh ``a2a-sdk`` client pointed at ``url``.

        We skip the ``.well-known/agent-card.json`` resolution step
        (``create_from_url``) because:

        1. Both peers run the same Alice build; the card at the other
           end is predictable and we gain nothing from fetching it.
        2. Card resolution over the WireGuard mesh is a round-trip we'd
           rather spare on every send. Minimal cards work for JSON-RPC.
        3. If a future peer is a non-Alice A2A agent, the operator can
           flip to full card resolution via a follow-up knob without
           reworking this client.
        """
        if self._httpx is None:
            headers: dict[str, str] = {}
            if self._identity_token:
                headers[PEER_TOKEN_HEADER] = self._identity_token
            # httpx.AsyncClient defaults to no overall timeout; the
            # per-call asyncio.timeout above owns that. Keep the
            # connect/read defaults reasonable so a stuck peer doesn't
            # hang the pool.
            self._httpx = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=None,
                    write=5.0,
                    pool=5.0,
                ),
            )
        config = ClientConfig(
            httpx_client=self._httpx,
            streaming=True,
            supported_protocol_bindings=[TransportProtocol.JSONRPC],
        )
        factory = ClientFactory(config)
        card = minimal_agent_card(
            url=url, transports=[TransportProtocol.JSONRPC]
        )
        return factory.create(card)


__all__ = [
    "A2AClient",
    "A2AReply",
    "PEER_TOKEN_HEADER",
    "resolve_identity_token",
    "resolve_client_timeout",
]
