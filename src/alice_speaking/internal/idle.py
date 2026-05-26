"""Idle-flush watcher — silent session-close turn after inbound silence.

Issue #373 / design at
``cortex-memory/research/2026-04-29-session-close-flush-design.md``.

When a conversational channel goes quiet for ``session_close_timeout_minutes``
minutes (default 10, hot-reloadable from ``alice.config.json``), this
source emits a single :class:`IdleEvent` for that ``(transport, address)``
pair. The dispatcher routes it to
:func:`alice_speaking._dispatch.handle_idle`, which runs a silent internal
turn (``silent=True``) asking the kernel to drop any open observations
into ``inner/notes/`` (or threshold-grade insights into ``inner/surface/``)
via the existing ``append_note`` tool. The turn has no outbound channel
and no ``send_message`` budget — its only job is to persist context that
would otherwise evaporate at the next compaction boundary.

State lives on the daemon (not on the source) because the inbound
touchpoints in :mod:`alice_speaking._dispatch` need to write the same
maps that this producer reads. ``_last_inbound`` is the
``(transport, address) → datetime`` map updated on every inbound;
``_idle_flushed`` is a set of keys this source has already fired for in
the current quiet window. Both reset for a key the next time that
``(transport, address)`` pair sees an inbound message — so the producer
fires once per silence gap, not on every poll cycle.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


# Poll cadence for the idle watcher. 60s is the design default — short
# enough that a freshly-stale conversation flushes within a minute of
# its timeout, long enough that an idle daemon doesn't burn cycles
# scanning a small dict on a tight loop.
IDLE_POLL_SECONDS = 60.0


@dataclass
class IdleEvent:
    """A conversational channel has gone silent for the configured timeout.

    Carries the originating principal's display name (resolved via the
    address book at producer time so the handler doesn't need to
    re-resolve), the transport label, and the timestamp of the last
    inbound on that channel. The handler computes the elapsed idle time
    from ``idle_since`` rather than from the producer's clock so the
    number reflects the moment the turn actually starts (which may be
    slightly later if the event sat in the queue).
    """

    sender_name: str
    transport: str
    idle_since: datetime.datetime


class IdleFlushSource:
    """Internal-source wrapper for the session-close flush protocol.

    Mirrors :class:`SurfaceWatcher` / :class:`EmergencyWatcher`: a
    long-running poll task on :meth:`producer`, a single-event
    :meth:`handle` that delegates to :func:`_dispatch.handle_idle`.

    No constructor arguments — the source reads everything it needs
    off the daemon via ``ctx`` (the proxy in
    :class:`alice_speaking.transports.base.DaemonContext`).
    """

    name = "idle_flush"
    event_type = IdleEvent

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Schedule the idle-watch loop.

        Returns the task so the daemon can supervise it under the same
        start/cancel semantics as any other producer.
        """
        return asyncio.create_task(self._run(ctx), name="idle-produce")

    async def _run(self, ctx: DaemonContext) -> None:
        """Poll ``_last_inbound`` every :data:`IDLE_POLL_SECONDS` and
        emit one :class:`IdleEvent` per channel that has been silent
        for longer than ``session_close_timeout_minutes``.

        Config is re-read on each cycle so a hot-reload of
        ``alice.config.json`` takes effect immediately. The default of
        10 minutes matches the design.
        """
        while not ctx._stop.is_set():
            await asyncio.sleep(IDLE_POLL_SECONDS)
            try:
                timeout = float(
                    ctx.cfg.speaking.get("session_close_timeout_minutes", 10)
                )
            except (TypeError, ValueError):
                # Bad config value — keep running with the default
                # rather than crash the producer.
                log.warning(
                    "session_close_timeout_minutes is not a number; "
                    "falling back to default 10"
                )
                timeout = 10.0
            cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(
                minutes=timeout
            )
            # Snapshot via ``list(...)`` so concurrent writes in the
            # inbound handlers don't trip ``dict changed size during
            # iteration``.
            for (transport, address), last_ts in list(ctx._last_inbound.items()):
                key = (transport, address)
                if last_ts < cutoff and key not in ctx._idle_flushed:
                    name = ctx.address_book.display_name_for(transport, address)
                    log.info(
                        "session idle for %s (%s/%s) since %s",
                        name,
                        transport,
                        address,
                        last_ts,
                    )
                    ctx._idle_flushed.add(key)
                    await ctx._queue.put(
                        IdleEvent(
                            sender_name=name,
                            transport=transport,
                            idle_since=last_ts,
                        )
                    )

    async def handle(self, ctx: DaemonContext, event: IdleEvent) -> None:
        """Run one silent flush turn for the idle channel."""
        # Late-bound to avoid a circular import: ``_dispatch`` references
        # :class:`IdleEvent` in its type hints; this module would
        # otherwise import ``_dispatch`` at module load.
        from .._dispatch import handle_idle

        await handle_idle(ctx, event)
