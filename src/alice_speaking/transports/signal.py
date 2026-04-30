"""SignalTransport: wraps :class:`SignalClient` under the Transport interface.

Owns Signal's full inbound pipeline end-to-end:

- :meth:`producer` returns one supervisor task that runs two inner
  loops: a *production* loop reading the Signal RPC (ACL gate +
  dedup + display-name lookup, then push to the per-transport
  inbox), and a *consumer* loop draining the inbox in same-sender
  bursts and running one kernel turn per burst. A daemon-level
  ``_turn_lock`` serialises with the main consumer so kernel state
  isn't shared across concurrent turns.
- :meth:`send` applies :func:`render` (markdown stripping + chunking
  via :data:`SIGNAL_CAPS`) before handing each chunk to
  :meth:`SignalClient.send`. Multi-chunk messages get a ``(i/N)``
  prefix so recipients can tell they go together. Attachments ride on
  chunk 1 only — same rule as :meth:`SignalClient.send`.
- :meth:`typing` delegates to the client's typing heartbeat.

Phase 2a of plan 01 moved batch coalescing here from
``SpeakingDaemon._drain_signal_batch``. Today's main consumer reaches
into the shared queue — that broke the "add a transport = one new
file" promise the moment a developer wrote a transport that bursts.
A per-transport inbox keeps that asymmetry inside Signal where it
belongs.

The class composes :class:`SignalClient` rather than reimplementing it —
``signal-cli``'s JSON-RPC, the receive log tail, the offset file, etc.
all stay where they are. Lifecycle (``wait_ready``, ``aclose``) stays
with the daemon for the same reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from ..infra.signal_rpc import SignalEnvelope
from .base import (
    SIGNAL_CAPS,
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
)


def _format_envelope_time(timestamp_ms: int) -> str:
    """Render a Signal envelope's millisecond Unix timestamp as a
    local time string. Used in multi-message prompts so Alice can
    see when each queued message arrived relative to the others.
    """
    try:
        dt = datetime.datetime.fromtimestamp(int(timestamp_ms) / 1000).astimezone()
    except (OSError, ValueError, OverflowError):
        return str(timestamp_ms)
    return dt.strftime("%-I:%M:%S %p %Z")


@dataclass
class SignalEvent:
    """One inbound Signal message, carried with the resolved sender display
    name so handlers and the prompt builder don't have to re-look it up.

    Lives next to :class:`SignalTransport` (Phase 2 / Plan 01 Phase 4): the
    transport produces this type, and the dispatcher routes by
    ``type(event)`` to :meth:`SignalTransport.handle`. Daemon code
    historically defined the dataclass; it's re-exported from
    ``alice_speaking.daemon`` for back-compat with callers and tests.
    """

    envelope: SignalEnvelope
    sender_name: str


log = logging.getLogger(__name__)


# Per-message state -> emoji. The daemon transitions an inbound through
# received -> replied | abandoned and the transport renders the state as
# a reaction on the originating message. Cosmetic feedback only — never
# load-bearing.
_STATE_EMOJI: dict[str, str] = {
    "received": "👀",
    "replied": "✅",
    "abandoned": "❌",
}


class SignalTransport:
    """Transport adapter for Signal. Wraps an existing :class:`SignalClient`."""

    name = "signal"
    caps: Capabilities = SIGNAL_CAPS
    event_type = SignalEvent

    def __init__(self, *, signal_client, inbox_size: int = 64) -> None:
        # Delayed import-style annotation: SignalClient lives in a sibling
        # module and importing it eagerly would create a cycle through
        # alice_speaking.daemon. Type-checkers can still see it via
        # `from .signal_client import SignalClient` at the call site.
        self._signal = signal_client
        # Per-transport inbox for inbound SignalEvents (Phase 2a of plan
        # 01). Keeps Signal's same-sender batching from reaching into
        # the daemon's shared queue.
        self._inbox: asyncio.Queue[SignalEvent] = asyncio.Queue(maxsize=inbox_size)

    # ------------------------------------------------------------------
    # Lifecycle (no-op — SignalClient lifecycle stays on the daemon)

    async def start(self) -> None:
        """No-op. The daemon owns ``signal_client.wait_ready()`` because
        the readiness check happens before any transports start.
        """
        return

    async def stop(self) -> None:
        """No-op. The daemon calls ``signal_client.aclose()`` itself
        during shutdown.
        """
        return

    # ------------------------------------------------------------------
    # Inbound — events flow through the per-transport inbox below, not
    # through :meth:`Transport.messages`. Signal's dedup + allowed-sender
    # + per-source batching don't fit the InboundMessage iterator shape.

    def messages(self) -> AsyncIterator[InboundMessage]:
        raise NotImplementedError(
            "SignalTransport doesn't expose messages() — its producer "
            "publishes SignalEvent onto a per-transport inbox the "
            "transport drains itself"
        )

    # ------------------------------------------------------------------
    # Prompt assembly (Phase 6c of plan 01)

    def build_prompt(
        self,
        *,
        sender_name: str,
        stamp: str,
        batch: list[SignalEvent],
    ) -> str:
        """Compose the per-turn prompt for one or more Signal envelopes.

        Body lives in
        ``alice_prompts/templates/speaking/turn.signal.md.j2``
        (Plan 04 Phase 5). Pre-flattens the batch into a list of
        ``{body, attachments, timestamp_str}`` dicts so the template
        owns the single-vs-multi rendering branch.
        """
        from alice_prompts import load as load_prompt
        from ..domain.render import capability_prompt_fragment

        messages = []
        for ev in batch:
            env = ev.envelope
            messages.append(
                {
                    "body": env.body or "(no text — see attachments below)",
                    "attachments": [
                        {
                            "path": att.path,
                            "content_type": att.content_type,
                            "filename": att.filename,
                        }
                        for att in env.attachments
                    ],
                    "timestamp_str": _format_envelope_time(env.timestamp),
                }
            )
        return load_prompt(
            "speaking.turn.signal",
            sender_name=sender_name,
            stamp=stamp,
            messages=messages,
            capability=capability_prompt_fragment("signal", SIGNAL_CAPS),
        )

    # ------------------------------------------------------------------
    # Dispatcher integration (Phase 2a of plan 01)

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Supervisor task that runs Signal's full inbound pipeline.

        Internally schedules two sub-tasks:

        - ``_produce`` reads the Signal RPC, gates by ACL + dedup, and
          pushes :class:`SignalEvent` objects onto :attr:`_inbox`.
        - ``_consume`` drains :attr:`_inbox` in same-sender bursts and
          runs one kernel turn per burst, holding ``ctx._turn_lock``
          across the pre-turn services + handler so it serialises with
          the daemon's main consumer.

        Returning a single supervisor task lets the daemon supervise
        Signal's pipeline with the same start/cancel semantics as any
        other transport. The two sub-tasks shut down together.
        """
        return asyncio.create_task(self._run(ctx), name="sig-produce")

    async def _run(self, ctx: DaemonContext) -> None:
        produce = asyncio.create_task(self._produce(ctx), name="sig-prod-inner")
        consume = asyncio.create_task(self._consume(ctx), name="sig-cons-inner")
        try:
            await asyncio.gather(produce, consume)
        except asyncio.CancelledError:
            for task in (produce, consume):
                task.cancel()
            for task in (produce, consume):
                with contextlib.suppress(BaseException):
                    await task
            raise

    async def _produce(self, ctx: DaemonContext) -> None:
        """Push raw Signal envelopes onto the per-transport inbox.

        Owns ACL gating (``ctx.address_book.is_allowed``), envelope
        dedup (``ctx.dedup``), and display-name resolution — all the
        per-source state that used to live in
        ``SpeakingDaemon._signal_producer``.
        """
        async for env in self._signal.receive():
            if not ctx.address_book.is_allowed("signal", env.source):
                log.info("ignoring envelope from %s", env.source)
                continue
            if ctx.dedup.seen(env.timestamp):
                log.debug("duplicate ts=%d; skipping", env.timestamp)
                continue
            ctx.dedup.mark(env.timestamp)
            sender_name = ctx.address_book.display_name_for("signal", env.source)
            await self._inbox.put(
                SignalEvent(envelope=env, sender_name=sender_name)
            )

    async def _consume(self, ctx: DaemonContext) -> None:
        """Drain the inbox in same-sender bursts and run one turn per burst."""
        from .._dispatch import handle_signal

        while True:
            head = await self._inbox.get()
            try:
                batch = self._drain_batch(head)
                async with ctx._turn_lock:
                    # Plan 01 Phase 6b routes the compaction policy
                    # through CompactionTrigger.should_run; pass the
                    # batch head so the deep-thread deferral hook (TODO
                    # — needs SessionDepthSignal) has the inbound event
                    # to inspect.
                    await ctx._pre_turn(head)
                    await handle_signal(ctx, batch)
            except Exception:  # noqa: BLE001
                log.exception("signal consume error")
            finally:
                self._inbox.task_done()

    def _drain_batch(self, head: SignalEvent) -> list[SignalEvent]:
        """Coalesce all currently-queued :class:`SignalEvent` objects from
        ``head``'s source into a batch.

        Best-effort coalescing: anything that arrives during the turn this
        batch produces will hit the next consumer iteration. Like Claude
        Code, queued input applies to the NEXT turn, not the current one.

        Events from a different sender stay on the inbox in their original
        order — they're popped, classified, and (when not part of the
        current batch) put back. Each ``get_nowait`` is matched by a
        ``task_done`` for queue-counter symmetry.
        """
        batch: list[SignalEvent] = [head]
        held: list[SignalEvent] = []
        while True:
            try:
                ev = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._inbox.task_done()
            if ev.envelope.source == head.envelope.source:
                batch.append(ev)
            else:
                held.append(ev)
        for ev in held:
            self._inbox.put_nowait(ev)
        return batch

    async def handle(self, ctx: DaemonContext, event: SignalEvent) -> None:
        """Run one turn for one Signal event.

        Reachable only through Phase 3's registry dispatch — Signal's
        own consumer loop drives turns in Phase 2a. Kept for protocol
        conformance and as the entry point an external caller (a test,
        a future replay tool) can use to drive a single signal turn
        without spinning up the inbox loop.
        """
        from .._dispatch import handle_signal

        await handle_signal(ctx, [event])

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> int:
        """Render Alice's text per :data:`SIGNAL_CAPS`, then send chunk-by-chunk.

        ``render()`` strips markdown (Signal renders none) and chunks to
        ``SIGNAL_CAPS.max_message_bytes`` (2000). Each chunk is delivered
        as a separate Signal message. When there are multiple chunks we
        prepend ``(i/N)`` so the recipient can see they go together. When
        there's only one chunk, no prefix — looks like a normal message.

        Attachments ride on the FIRST chunk only. signal-cli stores the
        attachment alongside the message; sending it on every chunk
        would multiply the upload and look like duplicate media.

        Returns the number of chunks delivered (0 when ``render`` produces
        an empty list — e.g. text was whitespace).
        """
        # Lazy import: alice_speaking.render imports transports.base, so
        # importing it at module scope would cycle through this module
        # back to render before render finishes initializing.
        from ..domain.render import render

        chunks = render(out.text, self.caps)
        if not chunks:
            log.debug("signal send: render produced no chunks; nothing to do")
            return 0

        recipient = out.destination.address
        attachments = list(out.attachments) if out.attachments else None
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            payload = f"({i}/{total}) {chunk}" if total > 1 else chunk
            # signal-cli's _CHUNK_LIMIT is 4000 chars and our chunks are
            # ≤2000 bytes (≤2000 chars in the worst case), so SignalClient
            # treats each call as one message and won't add its own (i/N).
            await self._signal.send(
                recipient,
                payload,
                attachments=attachments if i == 1 else None,
            )
        return total

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """Drive the typing-indicator heartbeat for a recipient."""
        if on:
            await self._signal.start_typing(channel.address)
        else:
            await self._signal.stop_typing(channel.address)

    async def set_message_state(
        self,
        channel: ChannelRef,
        target_timestamp: int,
        state: str,
    ) -> None:
        """React to a prior inbound message with the emoji for ``state``.

        Known states: ``received`` (we accepted it), ``replied`` (we sent
        a response), ``abandoned`` (we gave up — error or no reply).
        Unknown states are logged and skipped. Reactions are cosmetic;
        any RPC failure is swallowed rather than failing the turn.
        """
        emoji = _STATE_EMOJI.get(state)
        if not emoji:
            log.warning("unknown signal message state %r; skipping", state)
            return
        try:
            await self._signal.send_reaction(
                recipient=channel.address,
                target_author=channel.address,
                target_timestamp=target_timestamp,
                emoji=emoji,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "send_reaction failed (ts=%d state=%s): %s",
                target_timestamp,
                state,
                exc,
            )
