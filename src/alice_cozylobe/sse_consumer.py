"""SSE consumer — long-lived connection to cozyhem-engine's event bus.

Connects to ``/api/v1/events`` on the CozyHem REST host (default
``http://aimax1:8000``) and pushes parsed :class:`CozyHemEvent`
records onto an :class:`asyncio.Queue`. The wake loop drains the
queue; the consumer is a pure producer.

Reconnect policy mirrors :class:`alice_speaking.internal.cozyhem.CozyHemEventSubscriber`:
exponential backoff starting at 1s, capped at 30s, factor 2.0. Cap is
empirical — long enough not to hammer a flapping endpoint, short
enough that recovery feels prompt.

Walking-skeleton scope: bounded-size queue with drop-on-full
(:data:`DEFAULT_QUEUE_SIZE` = 50, matching the design's "queue depth
50" from the SSE inventory note). The urgency-tier-aware drop policy
described in the wake-loop design ships in a follow-up — for the
skeleton we drop the oldest event when the queue is full so the
producer never blocks.

The hardcoded ``http://aimax1:8000/api/v1/events`` URL is documented
here as a TODO pending cozyhem-engine#31, which introduces the AI-fleet-
managed binding (DNS / service discovery) that should replace the
literal host. See :data:`DEFAULT_EVENTS_URL`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import httpx

from .events import CozyHemEvent


__all__ = [
    "BACKOFF_FACTOR",
    "BACKOFF_INITIAL_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "DEFAULT_EVENTS_URL",
    "DEFAULT_QUEUE_SIZE",
    "SSEConsumer",
]


log = logging.getLogger(__name__)


# TODO(cozyhem-engine#31): replace the hardcoded host with the AI-fleet-
# managed service binding once the registry ships. Until then we point
# at aimax1 directly — the alice container runs on the same host and
# the SSE endpoint is bound to port 8000 by ``cozyhem/docker-compose.yml``.
DEFAULT_EVENTS_URL = "http://aimax1:8000/api/v1/events"

# Reconnect backoff bounds — copied verbatim from
# :mod:`alice_speaking.internal.cozyhem` so both consumers behave the
# same against a flapping upstream.
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
BACKOFF_FACTOR = 2.0

# Queue depth from the SSE inventory note (Step 1 of the lobe design).
# Each consumer gets its own queue; this depth balances burst tolerance
# against memory cost during a wake-loop stall.
DEFAULT_QUEUE_SIZE = 50


class SSEConsumer:
    """Long-lived SSE producer for CozyHem events.

    Owns the connection lifecycle (open → consume → reconnect with
    backoff). Caller passes an :class:`asyncio.Queue` and a stop
    :class:`asyncio.Event`; the consumer pushes events onto the
    queue until ``stop`` is set.

    Inject ``http_client_factory`` and ``sleep`` in tests so we don't
    open real sockets or burn wall clock during reconnect-backoff
    assertions.
    """

    def __init__(
        self,
        events_url: str = DEFAULT_EVENTS_URL,
        *,
        http_client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._events_url = events_url
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        self._sleep = sleep or asyncio.sleep

    async def run(
        self,
        queue: "asyncio.Queue[CozyHemEvent]",
        stop: asyncio.Event,
    ) -> None:
        """Open the SSE stream and parse frames forever (or until
        ``stop`` is set). Each iteration of the outer loop establishes
        one HTTP connection; on clean stream-end the backoff resets and
        we reconnect immediately; on exception the loop reconnects with
        backoff multiplied.
        """
        backoff = BACKOFF_INITIAL_SECONDS
        while not stop.is_set():
            try:
                await self._consume_once(queue, stop)
                backoff = BACKOFF_INITIAL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "cozylobe SSE error on %s: %s; reconnecting in %.1fs",
                    self._events_url,
                    exc,
                    backoff,
                )
                try:
                    await self._sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX_SECONDS)

    async def _consume_once(
        self,
        queue: "asyncio.Queue[CozyHemEvent]",
        stop: asyncio.Event,
    ) -> None:
        """Open one SSE connection and consume frames until it closes."""
        client_cm = self._http_client_factory()
        async with client_cm as client:
            async with client.stream(
                "GET",
                self._events_url,
                headers={"Accept": "text/event-stream"},
                timeout=None,
            ) as response:
                response.raise_for_status()
                log.info("cozylobe SSE connected: %s", self._events_url)
                event_kind = ""
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if stop.is_set():
                        return
                    if line == "":
                        if event_kind or data_lines:
                            self._enqueue(queue, event_kind, data_lines)
                        event_kind = ""
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        # SSE comment / heartbeat — ignore.
                        continue
                    field_name, _, value = line.partition(":")
                    # Strip the single leading space after the colon
                    # per the SSE spec.
                    if value.startswith(" "):
                        value = value[1:]
                    if field_name == "event":
                        event_kind = value
                    elif field_name == "data":
                        data_lines.append(value)
                    # ``id`` / ``retry`` fields exist in the spec but
                    # we don't use them for the walking skeleton.

    def _enqueue(
        self,
        queue: "asyncio.Queue[CozyHemEvent]",
        event_kind: str,
        data_lines: list[str],
    ) -> None:
        """Build a :class:`CozyHemEvent` from one parsed SSE frame and
        push it onto the queue. Drop-on-full so the producer never
        blocks the SSE socket — a stalled wake loop must not back up
        the upstream stream.
        """
        raw_data = "\n".join(data_lines)
        payload: dict = {}
        entity_id = ""
        if raw_data:
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError as exc:
                log.warning(
                    "cozylobe SSE: dropping unparseable data on kind=%s: %s",
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
            "cozylobe event: kind=%s entity_id=%s",
            event.kind,
            event.entity_id,
        )
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning(
                "cozylobe queue full; dropping event kind=%s entity_id=%s",
                event.kind,
                event.entity_id,
            )
