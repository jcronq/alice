"""CozyHem event subscriber — internal source for CozyHem's SSE event stream.

CozyHem (the home-control layer) exposes a Server-Sent Events stream
at ``/api/v1/events``. Every typed event the platform emits — doorbell
presses, light state changes, sensor triggers, etc. — flows out of
that one endpoint. This subscriber opens a long-lived SSE connection,
parses each frame into a typed :class:`CozyHemEvent`, and pushes it
onto the dispatcher queue. The dispatcher then routes the event
through :func:`alice_speaking._dispatch.handle_cozyhem_event` for
per-kind reaction.

First concrete consumer is the doorbell press from cozyhem-engine
PR 2: ``kind == "doorbell_pressed"`` → notify Jason on his preferred
channel. The shape is intentionally generic so future event kinds
(motion, light state, sensor) plug in without touching the producer
loop — only the dispatcher handler grows.

Mirrors :class:`SurfaceWatcher` / :class:`EmergencyWatcher` shape so
the registry can route by ``event_type``. Difference: no local
filesystem state — events live entirely in the upstream stream and
are pushed straight onto the queue once parsed.

Reconnect policy: exponential backoff on connection failure or
mid-stream error, starting at 1s, capped at 30s, factor 2.0. Cap is
empirical — long enough to avoid hammering a flapping endpoint, short
enough that recovery feels prompt once CozyHem comes back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


# Backoff bounds for SSE reconnect. Start small so a transient blip
# recovers immediately; cap so a long outage doesn't melt CozyHem.
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
BACKOFF_FACTOR = 2.0


@dataclass(frozen=True)
class CozyHemEvent:
    """One event lifted off CozyHem's SSE stream.

    Generic on purpose: the producer doesn't know what each ``kind``
    means semantically, only how to read the wire format. The
    dispatcher handler is where per-kind logic lives. ``payload`` is
    whatever JSON object the upstream attached — schema is owned by
    cozyhem-engine and varies by kind.

    Attributes:
        kind: The SSE ``event:`` line value. For example
            ``"doorbell_pressed"`` for the first known consumer.
            Unknown kinds get logged + dropped by the handler.
        entity_id: The CozyHem entity id this event originated from
            (e.g. ``"doorbell.front_door"``). Empty string when the
            event isn't tied to a specific entity.
        payload: Parsed JSON ``data:`` body. Empty dict when the wire
            data wasn't valid JSON; that path also logs a warning so
            we notice upstream regressions.
        received_at: ``time.time()`` at the moment the producer
            finished parsing the frame. Useful for staleness checks
            in handlers that fire asynchronously after a queue
            buildup.
    """

    kind: str
    entity_id: str
    payload: dict = field(default_factory=dict)
    received_at: float = 0.0


class CozyHemEventSubscriber:
    """Internal-source wrapper for the CozyHem SSE consumer.

    Owns the producer loop (:meth:`producer`) and the per-event
    dispatch shape required by :class:`InternalSource`. No
    bookkeeping equivalent to ``_dispatched``: each SSE frame is a
    one-shot upstream emission, not a file we might pick up twice.

    Construct once at daemon boot; register with the source registry.
    The factory takes the instance via ``cozyhem_subscriber=`` and
    routes :class:`CozyHemEvent` to :meth:`handle`.
    """

    name = "cozyhem"
    event_type = CozyHemEvent

    def __init__(
        self,
        events_url: str,
        *,
        http_client_factory=None,
        sleep=None,
    ) -> None:
        """Args:
            events_url: Absolute URL of the CozyHem SSE endpoint, e.g.
                ``http://aimax1:8000/api/v1/events``. The default
                lives in :mod:`alice_speaking.infra.config`; the
                daemon passes the resolved value in.
            http_client_factory: Optional callable returning an
                ``httpx.AsyncClient`` (or a compatible object). Tests
                inject a stub here; production lets the default
                ``httpx.AsyncClient`` build with no args.
            sleep: Optional async-callable replacement for
                ``asyncio.sleep`` so tests can verify backoff timing
                without burning wall clock.
        """
        self._events_url = events_url
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        self._sleep = sleep or asyncio.sleep

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Schedule the SSE consume loop. Returns the task so the
        daemon supervises it under the same start/cancel semantics
        as a transport's producer."""
        return asyncio.create_task(self._run(ctx), name="cozyhem-produce")

    async def _run(self, ctx: DaemonContext) -> None:
        """Open the SSE stream and parse frames forever.

        Each iteration of the outer loop establishes one HTTP
        connection. On clean stream-end the loop reconnects with
        backoff reset (the upstream may have rolled deliberately).
        On exception (network error, HTTP error, parse error mid-
        stream) the loop reconnects with backoff multiplied. The
        ``ctx._stop`` event short-circuits the loop so daemon
        shutdown isn't blocked by the long-poll.
        """
        backoff = BACKOFF_INITIAL_SECONDS
        while not ctx._stop.is_set():
            try:
                await self._consume_once(ctx)
                # Clean exit (server closed stream) — reset backoff
                # and reconnect immediately. The cap on the backoff
                # itself protects against a server that closes-and-
                # rejects in a tight loop.
                backoff = BACKOFF_INITIAL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "cozyhem SSE error on %s: %s; reconnecting in %.1fs",
                    self._events_url,
                    exc,
                    backoff,
                )
                try:
                    await self._sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX_SECONDS)

    async def _consume_once(self, ctx: DaemonContext) -> None:
        """Open one SSE connection and consume frames until it closes.

        ``httpx.AsyncClient.stream`` yields an async context manager
        that hands back the response object; ``aiter_lines`` then
        gives us one line at a time. The SSE format is
        ``event: <name>\\ndata: <json>\\n\\n`` — a blank line
        terminates one event. We buffer until that blank line, parse
        the captured kind / data fields, and push a
        :class:`CozyHemEvent` onto the queue.
        """
        client_cm = self._http_client_factory()
        async with client_cm as client:
            async with client.stream(
                "GET",
                self._events_url,
                headers={"Accept": "text/event-stream"},
                timeout=None,
            ) as response:
                response.raise_for_status()
                log.info(
                    "cozyhem SSE connected: %s", self._events_url
                )
                event_kind = ""
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if ctx._stop.is_set():
                        return
                    if line == "":
                        # Blank line: end of one SSE event. Build it,
                        # push it, reset buffers.
                        if event_kind or data_lines:
                            self._emit_event(ctx, event_kind, data_lines)
                        event_kind = ""
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        # SSE comment / heartbeat — ignore.
                        continue
                    field_name, _, value = line.partition(":")
                    # Strip a single leading space after the colon
                    # per the SSE spec.
                    if value.startswith(" "):
                        value = value[1:]
                    if field_name == "event":
                        event_kind = value
                    elif field_name == "data":
                        data_lines.append(value)
                    # ``id`` / ``retry`` lines are spec-defined but
                    # we don't need them for v1; let them fall
                    # through silently.

    def _emit_event(
        self,
        ctx: DaemonContext,
        event_kind: str,
        data_lines: list[str],
    ) -> None:
        """Build a :class:`CozyHemEvent` from one parsed SSE frame
        and push it onto the dispatcher queue.

        Parse failures on the data body don't kill the connection —
        we log + emit with an empty payload so the handler can decide
        what to do (typically log + drop, since a malformed payload
        is upstream's regression).
        """
        raw_data = "\n".join(data_lines)
        payload: dict = {}
        entity_id = ""
        if raw_data:
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError as exc:
                log.warning(
                    "cozyhem SSE: dropping unparseable data on kind=%s: %s",
                    event_kind,
                    exc,
                )
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
                entity_id_raw = parsed.get("entity_id", "")
                if isinstance(entity_id_raw, str):
                    entity_id = entity_id_raw
        event = CozyHemEvent(
            kind=event_kind,
            entity_id=entity_id,
            payload=payload,
            received_at=time.time(),
        )
        log.info(
            "cozyhem event: kind=%s entity_id=%s", event.kind, event.entity_id
        )
        try:
            ctx._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning(
                "cozyhem queue full; dropping event kind=%s entity_id=%s",
                event.kind,
                event.entity_id,
            )

    async def handle(self, ctx: DaemonContext, event: CozyHemEvent) -> None:
        """Route one :class:`CozyHemEvent` to its per-kind handler.

        Late-bound import of :mod:`_dispatch` to avoid the circular
        ``_dispatch`` → ``internal`` → ``_dispatch`` cycle (same
        pattern as :class:`BackgroundTaskCompletionSource`).
        """
        from .._dispatch import handle_cozyhem_event

        await handle_cozyhem_event(ctx, event)
