"""A2ATransport: Alice as a Google A2A protocol peer — both sides.

Lets external A2A-compliant agents submit text tasks to Alice (server
side) *and* lets Alice initiate tasks against peer A2A agents (client
side). Built on the official ``a2a-sdk``:

- Server: we implement an :class:`AgentExecutor` that bridges between
  A2A's per-task event-queue model and Alice's serial-consumer /
  Transport abstraction.
- Client: a thin :class:`alice_speaking.transports.a2a_client.A2AClient`
  wraps the SDK's JSON-RPC client. The :meth:`A2ATransport.send`
  method dispatches to it when the destination address is a URL
  (how peer principals register in ``principals.yaml``) instead of
  a server-side task id.

That shape gives Alice **symmetric** peering: a main-alice ↔ grand-alice
pairing where either side can initiate. Each inbound request carries
``X-Alice-Peer-Token``; the token→principal map resolves the caller to
the right principal (``main-alice`` vs. ``grand-alice``) so the turn
log reads correctly.

Bridge shape
------------

Inbound: the SDK calls :meth:`_AliceExecutor.execute` per submitted
task. We translate ``context.message`` to an :class:`InboundMessage`
and push it into the transport's inbox; the daemon's serial consumer
picks it up via :meth:`A2ATransport.messages` (same path as CLI /
Discord). A per-task ``asyncio.Queue`` lets the daemon stream chunks
back through :meth:`A2ATransport.send`.

Outbound (this turn's reply): chunks from the kernel turn arrive on
the per-task queue inside ``execute()``, get translated to
``TaskArtifactUpdateEvent`` (text artifacts) and ``TaskStatusUpdateEvent``
(WORKING → COMPLETED), and streamed to the client via the SDK's
``EventQueue``.

Outbound (peer-initiated by us): the outbox router hands a
``ChannelRef(transport="a2a", address="http://...")`` to
:meth:`send`. We recognise the URL and delegate to the :class:`A2AClient`,
which streams the peer's reply back as an :class:`A2AReply`. The
aggregated reply text rides back with the send-call's return so the
``send_message`` tool result includes what the peer said — Alice sees
her own ping and the pong in the same turn.

Auth (v1)
---------

All A2A traffic is authenticated by a shared-secret HTTP header,
``X-Alice-Peer-Token`` (:data:`alice_speaking.transports.a2a_client.PEER_TOKEN_HEADER`).
Config carries two inputs:

- ``identity_token`` — what WE present on outbound.
- ``peer_tokens`` — map of inbound tokens → principal_id. An inbound
  request whose token matches one of these entries gets its sender
  resolved to that principal; unauthenticated or unknown-token
  requests fall back to the single ``principal_name`` (default
  ``"a2a"``) same as Phase 1.

For deployments that don't peer (no YAML, no env vars) nothing
changes — the shared-secret path is opt-in and the server keeps
accepting unauthenticated callers as the catch-all principal. A
future upgrade path (oauth2-proxy / mTLS) can replace the token
without rewriting the executor.

Server lifecycle
----------------

:meth:`start` brings up a uvicorn server in a background task; the
agent card lives at ``/.well-known/agent-card.json`` and JSON-RPC
routes at ``/``. :meth:`stop` flips uvicorn's ``should_exit`` and
awaits the task. Bind host defaults to ``0.0.0.0`` so the worker's
host port mapping reaches the listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import uvicorn
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_artifact_update_event,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from starlette.applications import Starlette

from .a2a_client import A2AClient, A2AReply, PEER_TOKEN_HEADER
from .base import (
    A2A_CAPS,
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)


def _destination_is_url(address: str) -> bool:
    """Return True when ``address`` looks like an outbound peer URL.

    Server-side task ids (uuids) never contain ``://`` so the check is
    unambiguous. Address-book principals that register a peer Alice do
    so with a full URL (``http://10.200.0.2:7878``); the router hands
    that address through verbatim in the :class:`ChannelRef`.
    """
    if not address:
        return False
    lowered = address.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


# --- Workaround for a2a-sdk issue #1011 ---
# a2a-sdk 1.0.2's proto_utils uses `FieldDescriptor.label`, which was removed
# from modern protobuf (>=5.x). Every streaming request hits this and 500s
# with `'FieldDescriptor' object has no attribute 'label'`. Upstream marked
# this with a TODO to migrate to `field.is_repeated`; until they ship that,
# patch the two affected functions in place. Remove this block once a2a-sdk
# pins a protobuf version that still has `.label` or migrates the calls.
def _install_proto_utils_patch() -> None:
    from google.protobuf.descriptor import FieldDescriptor
    from a2a.utils import proto_utils as _pu

    def _check_required_field_violation(msg, field):
        val = getattr(msg, field.name)
        if field.is_repeated:
            if not val:
                return _pu.ValidationDetail(
                    field=field.name,
                    message="Field must contain at least one element.",
                )
        elif field.has_presence:
            if not msg.HasField(field.name):
                return _pu.ValidationDetail(
                    field=field.name, message="Field is required."
                )
        elif val == field.default_value:
            return _pu.ValidationDetail(field=field.name, message="Field is required.")
        return None

    def _recurse_validation(msg, field):
        errors: list = []
        if field.type != FieldDescriptor.TYPE_MESSAGE:
            return errors
        val = getattr(msg, field.name)
        if not field.is_repeated:
            if msg.HasField(field.name):
                sub_errs = _pu._validate_proto_required_fields_internal(val)
                _pu._append_nested_errors(errors, field.name, sub_errs)
        elif field.message_type.GetOptions().map_entry:
            for k, v in val.items():
                from google.protobuf.message import Message as ProtobufMessage

                if isinstance(v, ProtobufMessage):
                    sub_errs = _pu._validate_proto_required_fields_internal(v)
                    _pu._append_nested_errors(errors, f"{field.name}[{k}]", sub_errs)
        else:
            for i, item in enumerate(val):
                sub_errs = _pu._validate_proto_required_fields_internal(item)
                _pu._append_nested_errors(errors, f"{field.name}[{i}]", sub_errs)
        return errors

    _pu._check_required_field_violation = _check_required_field_violation
    _pu._recurse_validation = _recurse_validation


_install_proto_utils_patch()
# --- end workaround ---


# Single artifact stream per task — the A2A spec lets us emit multiple
# named artifacts but Alice's reply is a single text stream. The
# artifact name surfaces in some clients as a label.
_REPLY_ARTIFACT = "reply"


@dataclass
class A2AEvent:
    """An inbound A2A task wrapped for the dispatcher.

    Each event corresponds to one A2A task; the channel is ephemeral
    (lives for the duration of the SSE stream). Like :class:`CLIEvent`,
    but the daemon must explicitly call ``signal_done`` / ``signal_error``
    on the transport at end-of-turn so the SDK can close the SSE stream
    with a terminal status update. Re-exported from
    ``alice_speaking.daemon`` for back-compat.
    """

    message: InboundMessage


class A2ATransport:
    """A2A protocol server transport — inbound only in Phase 1.

    Construction does not bind the port; call :meth:`start`. The class
    keeps no global state beyond the per-task outbox map, so multiple
    in-flight tasks don't interfere.
    """

    name = "a2a"
    caps: Capabilities = A2A_CAPS
    event_type = A2AEvent

    def __init__(
        self,
        *,
        port: int,
        principal_name: str = "a2a",
        principal_display_name: str = "A2A peer agent",
        agent_name: str = "Alice",
        agent_description: str = "Personal AI agent — A2A endpoint.",
        agent_version: str = "0.1.0",
        host: str = "0.0.0.0",
        external_url: Optional[str] = None,
        inbox_size: int = 64,
        peer_tokens: Optional[dict[str, str]] = None,
        peer_display_names: Optional[dict[str, str]] = None,
        identity_token: Optional[str] = None,
        client_timeout_s: Optional[float] = None,
    ) -> None:
        self._port = port
        self._host = host
        # external_url is what we advertise on the agent card. Container-
        # internal :host:port is fine for dev; behind a reverse proxy
        # the operator should set ALICE_A2A_EXTERNAL_URL so peers know
        # the publicly-reachable endpoint.
        self._external_url = external_url or f"http://{host}:{port}/"
        self._principal_name = principal_name
        self._principal_display_name = principal_display_name
        self._agent_name = agent_name
        self._agent_description = agent_description
        self._agent_version = agent_version
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        self._outbox: dict[str, asyncio.Queue[dict]] = {}
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._server_task: Optional[asyncio.Task] = None
        # Peer-token → principal-id map. When an inbound request carries
        # a known token in ``X-Alice-Peer-Token``, :class:`_AliceExecutor`
        # tags the InboundMessage's principal with the matching id so
        # downstream handling attributes the turn to the correct peer
        # (e.g. "main-alice" vs. "grand-alice") instead of the single
        # catch-all ``principal_name``. Unknown / missing tokens keep
        # the Phase-1 fallback behaviour.
        self._peer_tokens: dict[str, str] = dict(peer_tokens or {})
        # Optional display-name overrides for peer principals. Lets
        # ``principals.yaml`` carry the authoritative display name; the
        # transport prefers it over the auto-built
        # ``{principal_id} (a2a peer)`` label.
        self._peer_display_names: dict[str, str] = dict(peer_display_names or {})
        # Outbound A2A client — lazy inner construction so tests that
        # don't exercise outbound can instantiate the transport without
        # touching httpx. Shared across every peer URL; the client
        # itself caches per-URL SDK clients internally.
        self._client = A2AClient(
            identity_token=identity_token,
            default_timeout_s=client_timeout_s,
        )

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        agent_card = self._build_agent_card()
        executor = _AliceExecutor(self)
        request_handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=InMemoryTaskStore(),
            agent_card=agent_card,
        )
        routes: list = []
        routes.extend(create_agent_card_routes(agent_card))
        # enable_v0_3_compat=True: signal_dispatch.forward_to_a2a builds
        # v0.3-style `message/send` payloads. Without this flag the A2A
        # SDK returns MethodNotFound for every forward and the inbound
        # never produces a turn. Keep the flag until the forwarder is
        # migrated to the v1 method name, then revisit.
        routes.extend(
            create_jsonrpc_routes(request_handler, "/", enable_v0_3_compat=True)
        )
        app = Starlette(routes=routes)

        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            # uvicorn installs its own signal handlers by default, which
            # collides with the daemon's SIGTERM/SIGINT handling. Disable
            # so the daemon's stop path drives shutdown via .stop().
            lifespan="off",
        )
        self._uvicorn_server = uvicorn.Server(config)
        # Don't override our caller's signal handlers either.
        self._uvicorn_server.install_signal_handlers = lambda: None
        self._server_task = asyncio.create_task(
            self._uvicorn_server.serve(), name="a2a-server"
        )

        # Wait until uvicorn has actually bound the socket. Without this,
        # daemon_ready can fire before the port is accepting connections,
        # and a fast client gets ECONNREFUSED. 5s is generous; uvicorn
        # binds in tens of milliseconds normally.
        for _ in range(50):
            if self._uvicorn_server.started:
                break
            if self._server_task.done():
                # uvicorn died during startup — surface the error
                self._server_task.result()
            await asyncio.sleep(0.1)
        if not self._uvicorn_server.started:
            raise RuntimeError(
                f"a2a transport: uvicorn failed to bind {self._host}:{self._port} "
                f"within 5s"
            )
        log.info(
            "A2A transport listening on %s:%d (advertised: %s)",
            self._host,
            self._port,
            self._external_url,
        )

    async def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._server_task
        self._uvicorn_server = None
        self._server_task = None
        # Drain outbound sockets. Swallow — the daemon's shutdown path
        # should never raise through transport.stop.
        with contextlib.suppress(Exception):
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Inbound

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self._inbox.get()

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> int:
        """Deliver ``out`` to the right side of A2A.

        Two modes, chosen by the destination address shape:

        - **Server-mode push** — the address is a server-side task id
          from an active inbound. We push rendered chunks onto the
          per-task outbox so the :class:`_AliceExecutor.execute` loop
          translates them into A2A artifact events on the SSE stream
          this turn is replying to.
        - **Client-mode POST** — the address is an ``http(s)://`` URL
          (how peer principals in ``principals.yaml`` register). We
          hand the text to :class:`A2AClient` which runs the full
          JSON-RPC send-and-stream against the peer. The aggregated
          reply text is logged for operator visibility; the chunk
          count returned matches the reply-side chunk count so the
          send-event emitter stays uniform with other transports.

        A dropped delivery (no live task on the server side, or an
        error / timeout on the client side) returns 0 chunks — same
        shape as CLI's "no live connection" path, keeping the
        ``*_send`` event schema stable.
        """
        from ..domain.render import render

        destination = out.destination.address
        if _destination_is_url(destination):
            reply = await self._client.send_text(url=destination, text=out.text)
            self._log_client_reply(destination, out.text, reply)
            # Chunk-count: 1 per artifact frame we received back. For the
            # ``quick ping``-style ping/pong this is usually 1; for
            # longer peer replies the counter tracks the stream shape.
            return reply.chunk_count or (0 if reply.error or reply.timed_out else 1)

        task_id = destination
        q = self._outbox.get(task_id)
        if q is None:
            log.warning(
                "a2a send: no live task for %s; dropping %d chars",
                task_id,
                len(out.text),
            )
            return 0
        chunks = render(out.text, self.caps)
        if not chunks:
            return 0
        for chunk in chunks:
            q.put_nowait({"kind": "chunk", "text": chunk})
        return len(chunks)

    def _log_client_reply(
        self, url: str, sent: str, reply: A2AReply
    ) -> None:
        """Structured log line for one outbound A2A exchange.

        The outbound call is fire-and-wait — the daemon doesn't stream
        the reply into Alice's turn (that would require inverting the
        turn-runner's ``send_message`` call chain). Instead the reply
        text rides in this log line + the send-event, which operators
        can grep for.
        """
        if reply.error:
            log.warning(
                "a2a outbound error: url=%s sent_len=%d err=%s",
                url,
                len(sent),
                reply.error,
            )
            return
        if reply.timed_out:
            log.warning(
                "a2a outbound timed out: url=%s sent_len=%d partial=%d chars",
                url,
                len(sent),
                len(reply.text),
            )
            return
        log.info(
            "a2a outbound ok: url=%s sent_len=%d reply_len=%d chunks=%d state=%s",
            url,
            len(sent),
            len(reply.text),
            reply.chunk_count,
            reply.state,
        )

    async def send_and_capture(
        self, url: str, text: str, timeout_s: Optional[float] = None
    ) -> A2AReply:
        """Public hook for callers that want the aggregated peer reply.

        The :meth:`send` path returns a chunk count because the
        :class:`Transport` protocol constrains it to; callers that want
        the reply text (e.g. a mutual-ping smoke test, a future tool
        that lets Alice *see* the peer's reply inline) go through here.
        """
        return await self._client.send_text(url=url, text=text, timeout_s=timeout_s)

    def register_peer(
        self,
        *,
        principal_id: str,
        token: str,
        display_name: Optional[str] = None,
    ) -> None:
        """Teach the server side about a peer's shared-secret token.

        Idempotent — re-registering the same token re-binds it. Called
        by the daemon at startup once ``a2a-peers.yaml`` + the
        env-sourced token values are resolved. Kept as a method so
        tests can wire a test peer without rebuilding the transport.
        """
        if not token or not principal_id:
            raise ValueError("register_peer requires non-empty token + principal_id")
        self._peer_tokens[token] = principal_id
        if display_name:
            self._peer_display_names[principal_id] = display_name

    def _principal_for_token(self, token: Optional[str]) -> tuple[str, str]:
        """Resolve an inbound ``X-Alice-Peer-Token`` to a principal.

        Falls back to the configured catch-all when the token is
        missing or unknown; the result is used verbatim on
        :class:`Principal.native_id` so the address book can map the
        turn back to a ``principals.yaml`` entry.
        """
        if token:
            principal_id = self._peer_tokens.get(token)
            if principal_id:
                display = (
                    self._peer_display_names.get(principal_id)
                    or f"{principal_id} (a2a peer)"
                )
                return principal_id, display
        return self._principal_name, self._principal_display_name

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        # No-op: A2A status updates fill the same role.
        return

    # ------------------------------------------------------------------
    # Prompt assembly (Phase 6c of plan 01)

    def build_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """Compose the prompt for a single A2A task.

        Body lives in
        ``prompts/templates/speaking/turn.a2a.md.j2``
        (Plan 04 Phase 5).

        When ``metadata`` carries a ``signal_sender`` key (the
        sender-based dispatch path — see
        :mod:`alice_speaking.signal_dispatch`) the template renders a
        Signal-forwarded framing that steers Alice to reply by calling
        ``send_message`` with the original Signal recipient, so her
        reply reaches the sender over Signal instead of streaming back
        on the A2A task.
        """
        from prompts import load as load_prompt
        from ..domain.render import capability_prompt_fragment

        meta = metadata or {}
        return load_prompt(
            "speaking.turn.a2a",
            principal_name=principal_name,
            stamp=stamp,
            text=text,
            capability=capability_prompt_fragment("a2a", self.caps),
            signal_sender=meta.get("signal_sender") or "",
            signal_sender_name=meta.get("signal_sender_name") or "",
            dispatch_label=meta.get("dispatch_label") or "",
        )

    # ------------------------------------------------------------------
    # Dispatcher integration (Phase 2 of plan 01)

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Pump per-task :class:`InboundMessage` objects from the SDK
        executor onto ``ctx._queue`` as :class:`A2AEvent` events.

        v1: all A2A traffic shares a single configured principal
        (the transport attaches it when building the InboundMessage),
        so there's no per-caller ACL gate here. Auth, when needed,
        lives upstream of the worker (proxy / ingress)."""
        return asyncio.create_task(self._produce(ctx), name="a2a-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        async for msg in self.messages():
            await ctx._queue.put(A2AEvent(message=msg))

    async def handle(self, ctx: DaemonContext, event: A2AEvent) -> None:
        """Run one turn for one A2A event. Phase 2 — declared for
        protocol conformance; the daemon's consumer still calls
        :func:`_dispatch.handle_a2a` directly until Phase 3."""
        from .._dispatch import handle_a2a

        await handle_a2a(ctx, event)

    # ------------------------------------------------------------------
    # Sentinels (called by the daemon's A2A handler at end-of-turn)

    async def signal_done(self, channel: ChannelRef) -> None:
        q = self._outbox.get(channel.address)
        if q is not None:
            q.put_nowait({"kind": "done"})

    async def signal_error(self, channel: ChannelRef, message: str) -> None:
        q = self._outbox.get(channel.address)
        if q is not None:
            q.put_nowait({"kind": "error", "message": message})

    async def push_lifecycle_event(self, channel: ChannelRef, event: dict) -> None:
        """No-op: A2A clients consume the SDK's structured task-status
        and artifact events, not raw lifecycle chunks. ``A2A_CAPS.lifecycle_events``
        is False so the handler short-circuits before this is reached
        — stub kept for protocol conformance."""
        return None

    # ------------------------------------------------------------------
    # Internals shared with _AliceExecutor

    def _open_task_outbox(self, task_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._outbox[task_id] = q
        return q

    def _close_task_outbox(self, task_id: str) -> None:
        self._outbox.pop(task_id, None)

    async def _push_inbound(self, inbound: InboundMessage) -> None:
        await self._inbox.put(inbound)

    def _make_principal(self, token: Optional[str] = None) -> Principal:
        """Build the :class:`Principal` for an inbound A2A task.

        When ``token`` matches a registered peer token the principal is
        pinned to that peer's id + display name so the turn log shows
        "from main-alice" rather than the single "a2a" catch-all. A
        missing / unknown token keeps the Phase-1 default behaviour.
        """
        principal_id, display = self._principal_for_token(token)
        return Principal(
            transport="a2a",
            native_id=principal_id,
            display_name=display,
        )

    def _build_agent_card(self) -> AgentCard:
        return AgentCard(
            name=self._agent_name,
            description=self._agent_description,
            version=self._agent_version,
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            capabilities=AgentCapabilities(streaming=True),
            skills=[
                AgentSkill(
                    id="conversation",
                    name="Conversation",
                    description="Talk to Alice in plain English. She has access to her mind repo, tools, and memory.",
                    tags=["chat", "assistant"],
                    examples=[
                        "What's on today?",
                        "Summarize the cortex-memory dailies for the past week.",
                    ],
                ),
            ],
            supported_interfaces=[
                AgentInterface(
                    protocol_binding="JSONRPC",
                    # Match the version the SDK's DefaultRequestHandler enforces
                    # on incoming A2A-Version headers. Advertising 0.3 here
                    # would have clients default to that and get rejected.
                    protocol_version="1.0",
                    url=self._external_url,
                ),
            ],
        )


class _AliceExecutor(AgentExecutor):
    """Per-task bridge between A2A's event-queue model and Alice's daemon.

    The SDK invokes :meth:`execute` once per submitted task. We:

    1. Translate the inbound A2A message → :class:`InboundMessage`.
    2. Push it onto the transport's inbox (the daemon's consumer reads
       from there serially).
    3. Open a per-task outbox queue. :meth:`A2ATransport.send` and the
       sentinel methods fill it as the kernel turn produces output.
    4. Pump that queue, translating each event to the matching A2A
       event (``TaskArtifactUpdateEvent`` for chunks,
       ``TaskStatusUpdateEvent`` with ``COMPLETED`` / ``FAILED`` for
       end-of-turn).
    5. Return when the task reaches a terminal state — the SDK closes
       the streaming response on our behalf.
    """

    def __init__(self, transport: A2ATransport) -> None:
        self._t = transport

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Create or pick up the Task. The SDK requires us to enqueue a
        # Task BEFORE any status / artifact updates — that's what
        # registers the task in the in-memory store and lets clients
        # poll/cancel it. ``current_task`` is set when the request
        # references an existing task id (resumes, follow-ups);
        # otherwise we mint a new one from the inbound message.
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        task_id = task.id
        ctx_id = task.context_id or task_id

        text = (
            (get_message_text(context.message) or "").strip() if context.message else ""
        )
        if not text:
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id,
                    context_id=ctx_id,
                    state=TaskState.TASK_STATE_FAILED,
                    text="empty message; nothing to do",
                )
            )
            return

        # Peer-token resolution — the Starlette-based context builder
        # that the SDK ships stashes HTTP headers into
        # ``call_context.state['headers']`` (keys lowercased). We pull
        # the Alice peer token out and resolve it to a principal before
        # building the InboundMessage so the daemon's address-book
        # ``learn`` path attributes the turn to the right peer.
        peer_token: Optional[str] = None
        call_ctx = getattr(context, "call_context", None)
        if call_ctx is not None:
            headers = call_ctx.state.get("headers") if call_ctx.state else None
            if isinstance(headers, dict):
                peer_token = headers.get(PEER_TOKEN_HEADER) or headers.get(
                    PEER_TOKEN_HEADER.upper()
                )

        # Carry a2a_context_id plus any caller-supplied metadata through
        # to the daemon. The sender-based dispatch path (see
        # :mod:`alice_speaking.signal_dispatch`) uses this to plumb the
        # originating Signal sender number + display name + dispatch
        # label into the kernel prompt so Alice can reply via Signal to
        # the actual sender rather than back over the A2A stream.
        inbound_metadata: dict = {"a2a_context_id": ctx_id}
        if context.message is not None and context.message.metadata:
            md = context.message.metadata
            # v0.3-compat path delivers metadata as a protobuf Struct,
            # v1 path as a dict. Coerce to dict so .get() works.
            if not isinstance(md, dict):
                try:
                    from google.protobuf.json_format import MessageToDict
                    md = MessageToDict(md, preserving_proto_field_name=True)
                except Exception:
                    md = {}
            for key in (
                "signal_sender",
                "signal_sender_name",
                "signal_timestamp",
                "dispatch_label",
            ):
                value = md.get(key)
                if value is not None:
                    inbound_metadata[key] = value

        outbox = self._t._open_task_outbox(task_id)
        try:
            inbound = InboundMessage(
                principal=self._t._make_principal(peer_token),
                origin=ChannelRef(transport="a2a", address=task_id, durable=False),
                text=text,
                timestamp=time.time(),
                metadata=inbound_metadata,
            )
            await self._t._push_inbound(inbound)

            # Tell the client we accepted the task. WORKING is non-final,
            # the SDK keeps the SSE stream open for subsequent updates.
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id,
                    context_id=ctx_id,
                    state=TaskState.TASK_STATE_WORKING,
                    text="thinking…",
                )
            )

            artifact_id = uuid.uuid4().hex
            first_chunk = True
            while True:
                ev = await outbox.get()
                kind = ev["kind"]
                if kind == "chunk":
                    await event_queue.enqueue_event(
                        new_text_artifact_update_event(
                            task_id=task_id,
                            context_id=ctx_id,
                            name=_REPLY_ARTIFACT,
                            text=ev["text"],
                            append=not first_chunk,
                            last_chunk=False,
                            artifact_id=artifact_id,
                        )
                    )
                    first_chunk = False
                elif kind == "done":
                    # Mark the artifact closed before flipping status —
                    # some clients use last_chunk to know they have the
                    # full reply.
                    if not first_chunk:
                        await event_queue.enqueue_event(
                            new_text_artifact_update_event(
                                task_id=task_id,
                                context_id=ctx_id,
                                name=_REPLY_ARTIFACT,
                                text="",
                                append=True,
                                last_chunk=True,
                                artifact_id=artifact_id,
                            )
                        )
                    await event_queue.enqueue_event(
                        new_text_status_update_event(
                            task_id=task_id,
                            context_id=ctx_id,
                            state=TaskState.TASK_STATE_COMPLETED,
                            text="",
                        )
                    )
                    break
                elif kind == "error":
                    await event_queue.enqueue_event(
                        new_text_status_update_event(
                            task_id=task_id,
                            context_id=ctx_id,
                            state=TaskState.TASK_STATE_FAILED,
                            text=ev["message"],
                        )
                    )
                    break
                else:
                    log.warning("a2a executor: unknown outbox event kind %r", kind)
        finally:
            self._t._close_task_outbox(task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # v1: best-effort. We acknowledge cancellation but the daemon's
        # in-flight turn keeps running until it finishes — turns are
        # short-ish and proper interrupt plumbing into the kernel can
        # come later. The next outbound chunk will be dropped (no live
        # outbox), and the daemon's signal_done will be a no-op.
        task_id = context.task_id or uuid.uuid4().hex
        ctx_id = context.context_id or task_id
        self._t._close_task_outbox(task_id)
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=ctx_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="cancellation acknowledged; in-flight turn may still complete",
            )
        )


__all__ = ["A2ATransport"]
