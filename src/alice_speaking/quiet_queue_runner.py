"""Background watcher that drains the quiet-hours queue.

Two responsibilities:

1. Poll quiet-hours state every ``CHECK_SECONDS``; on the
   transition from quiet → active, drain whatever queued up
   during the window.
2. Drain a queue (held messages → outbound dispatcher) once,
   either on the transition or at startup if quiet hours ended
   while the daemon was down.

Plan 01 Phase 6c lifts this out of ``SpeakingDaemon`` so the
daemon's run loop reads as orchestration only. The runner reaches
back through ctx for the outbound dispatcher and event emitter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .quiet_hours import QuietQueue, is_quiet_hours
from .transports import ChannelRef


log = logging.getLogger(__name__)


# Wall-clock poll interval. 30s lets the queue drain within a
# minute of the quiet window ending — slow enough not to thrash,
# fast enough that the user gets a queued reply in human time.
CHECK_SECONDS = 30.0


class QuietQueueRunner:
    """Owns the quiet-hours watch loop + manual-drain entry point.

    Constructed once in :class:`SpeakingDaemon.__init__` and reached
    via ``ctx`` from the daemon's run loop / startup phase.
    """

    def __init__(
        self,
        *,
        speaking_cfg: dict,
        quiet_queue: QuietQueue,
        events: Any,
        dispatch_outbound: Any,
        stop_event: asyncio.Event,
    ) -> None:
        # ``dispatch_outbound`` is a coroutine function with the
        # signature
        # ``(channel, text, attachments=None, *, turn_id=None,
        #    emergency=False, bypass_quiet=False) -> None``.
        # We accept a callable rather than the OutboxRouter directly
        # so the daemon can keep its principal-display-name plumbing
        # in one place.
        self._cfg = speaking_cfg
        self._queue = quiet_queue
        self._events = events
        self._dispatch_outbound = dispatch_outbound
        self._stop = stop_event

    # ------------------------------------------------------------------
    # Watch loop

    async def watch(self) -> None:
        """Poll quiet-hours state; drain on the transition out."""
        was_quiet = is_quiet_hours(self._cfg)
        while not self._stop.is_set():
            await asyncio.sleep(CHECK_SECONDS)
            now_quiet = is_quiet_hours(self._cfg)
            if was_quiet and not now_quiet:
                await self.drain(reason="quiet-hours-ended")
            was_quiet = now_quiet

    # ------------------------------------------------------------------
    # Manual drain (used at startup + by the watch loop)

    async def drain(self, *, reason: str) -> None:
        """Drain the quiet queue once, dispatching every held message
        with ``bypass_quiet=True`` so the (now non-quiet) clock
        doesn't re-queue them. Failed deliveries get re-queued with
        a logged exception."""
        messages = self._queue.drain()
        if not messages:
            return
        log.info("draining quiet queue (%d msgs) — %s", len(messages), reason)
        self._events.emit(
            "quiet_queue_drain", count=len(messages), reason=reason
        )
        for msg in messages:
            channel = ChannelRef(
                transport=msg.transport,
                address=msg.recipient,
                durable=True,
            )
            try:
                await self._dispatch_outbound(
                    channel,
                    msg.text,
                    bypass_quiet=True,  # past the window already
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to send queued %s message to %s; re-queueing",
                    msg.transport,
                    msg.recipient,
                )
                self._queue.append(msg)


__all__ = ["CHECK_SECONDS", "QuietQueueRunner"]
