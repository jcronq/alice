"""Synthetic completion event for background sub-agent dispatches.

The :class:`mcp__alice__dispatch_background_task` tool fires off a
sub-agent in an asyncio task, returns a handle to Alice immediately,
and lets her parent turn end naturally. When the sub-agent eventually
finishes, the daemon-owned waiter pushes a
:class:`BackgroundTaskCompleteEvent` onto the dispatcher queue.

The dispatcher routes that event through
:class:`BackgroundTaskCompletionSource` to
:func:`alice_speaking._dispatch.handle_background_task_complete`,
which starts a fresh turn so Alice can read the result and decide
what to forward to the originating channel (Signal/Discord/CLI).

This source has no producer — events are pushed by the tool's
asyncio task lifecycle, not polled. :meth:`producer` returns None
to signal that to the daemon's startup loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from ..transports.base import ChannelRef, DaemonContext


log = logging.getLogger(__name__)


@dataclass
class BackgroundTaskCompleteEvent:
    """A background sub-agent has finished; deliver the result to Alice.

    Attributes:
        handle: The ``bg-<uuid>`` correlation id returned to Alice when
            she dispatched the task. Lets her map the completion back
            to the user-facing promise she made at dispatch time.
        description: The short label Alice gave the task at dispatch.
            Surfaced in the new-turn prompt so she has context.
        result_text: The sub-agent's final text output. Empty when the
            sub-agent crashed or returned no text — :attr:`is_error`
            disambiguates.
        is_error: True if the sub-agent raised or returned an error
            result. The handler uses this to frame the prompt
            differently ("X failed:" vs "X completed:").
        channel: The originating reply channel captured at dispatch
            time. Restored on ``ctx._current_reply_channel`` for the
            new turn so Alice's ``send_message(recipient="self")``
            routes back to whoever originally asked.
        principal_name: Display name of the principal who originally
            triggered the dispatch. Surfaced in the prompt for
            context and used for outbound event tagging.
    """

    handle: str
    description: str
    result_text: str
    is_error: bool
    channel: Optional[ChannelRef]
    principal_name: str


class BackgroundTaskCompletionSource:
    """Internal-source wrapper for background-task completion routing.

    Mirrors :class:`SurfaceWatcher` / :class:`EmergencyWatcher` shape
    so the registry can route by ``event_type``. Difference: we have
    no producer (events are pushed directly by the tool's asyncio
    task callback), so :meth:`producer` returns None and the daemon
    skips supervision.
    """

    name = "background_tasks"
    event_type = BackgroundTaskCompleteEvent

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        # No producer loop — events arrive via direct queue.put() from
        # the dispatch_background_task tool's per-subagent waiter.
        return None

    async def handle(
        self, ctx: DaemonContext, event: BackgroundTaskCompleteEvent
    ) -> None:
        # Late-bound to avoid a circular import: _dispatch imports
        # this module's event class for type hints, and this module
        # would otherwise import _dispatch for the handler function.
        from .._dispatch import handle_background_task_complete

        await handle_background_task_complete(ctx, event)
