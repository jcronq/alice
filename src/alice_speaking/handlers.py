"""BlockHandlers for the speaking daemon.

Compose-time extensions to :class:`alice_core.kernel.AgentKernel` that
encode speaking-specific semantics the kernel doesn't know about:

- :class:`SessionHandler` — on each ``ResultMessage``, update the
  daemon's session_id and (unless silent) persist it to ``session.json``
  so the next process start can ``resume=`` warm.
- :class:`CompactionArmer` — on each ``ResultMessage``, arm the
  compaction flag if ``usage.input_tokens`` crossed the threshold.

The missed-reply detector is NOT a handler. Whether a turn produced
outbound is determined by whether Alice's ``send_message`` tool callback
fired on the daemon — not by observing ``tool_use`` blocks — because the
tool invocation could legally error out between block and callback. The
daemon still tracks that via ``self._turn_did_send``.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Callable

from alice_core.kernel import NullHandler
from alice_core import session as session_state

from . import compaction as compaction_module


log = logging.getLogger(__name__)


class SessionHandler(NullHandler):
    """Update the daemon's session_id on each turn's result.

    When ``persist=True``, also writes ``session.json`` so a process
    restart can resume warm. Silent turns (bootstrap, compaction) use
    ``persist=False`` — we still track the active session_id in memory
    so later turns pass ``resume=``, but we don't flap the file across a
    compaction roll.
    """

    def __init__(
        self,
        *,
        session_path: pathlib.Path,
        set_session_id: Callable[[str], None],
        persist: bool,
    ) -> None:
        self._session_path = session_path
        self._set_session_id = set_session_id
        self._persist = persist

    async def on_result(self, msg) -> None:
        if not msg.session_id:
            return
        self._set_session_id(msg.session_id)
        if not self._persist:
            return
        try:
            session_state.write(self._session_path, msg.session_id)
        except OSError:
            log.exception(
                "failed to persist session_id to %s", self._session_path
            )


class CompactionArmer(NullHandler):
    """Arm the daemon's compaction flag when ``input_tokens`` crosses
    the configured threshold.

    The flag is checked by the consumer loop *before the next event* —
    so the current turn always completes normally; compaction happens in
    the gap between turns, not mid-turn.
    """

    def __init__(
        self,
        *,
        threshold: int,
        arm: Callable[[], None],
    ) -> None:
        self._threshold = threshold
        self._arm = arm

    async def on_result(self, msg) -> None:
        if not msg.usage:
            return
        if compaction_module.should_compact(msg.usage, self._threshold):
            self._arm()
            log.info(
                "compaction armed (input_tokens=%s > threshold=%d)",
                msg.usage.get("input_tokens"),
                self._threshold,
            )


__all__ = ["SessionHandler", "CompactionArmer"]
