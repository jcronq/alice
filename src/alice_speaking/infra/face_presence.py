"""Push Speaking's current lifecycle state to the alice-face ESP32.

Mapping driven by the daemon's per-turn lifecycle:

- ``idle``      — no turn in flight (default between turns).
- ``thinking``  — turn lock held: she's processing an inbound (reading,
                  tool-using, drafting a reply).
- ``speaking``  — :meth:`SpeakingDaemon._send_message` just dispatched an
                  outbound. Held briefly via a context manager and falls
                  back to ``thinking`` while the turn continues, then to
                  ``idle`` when the turn returns.
- ``sleep``     — quiet hours active and no turn in flight.

Best-effort: every state push is fire-and-forget with a tight timeout.
If the face is offline / unreachable / wedged, the daemon never blocks
and never raises — observability and presence are convenience, not
correctness. The state push runs as an asyncio task so it does not
serialize with the turn loop.

Wire-up:

- :meth:`set_state` schedules an HTTP POST and returns immediately.
- :meth:`speaking` is an async context manager: pushes ``speaking`` on
  enter and falls back to ``thinking`` on exit. Used around the actual
  send in ``_send_message``.
- :meth:`set_idle` chooses between ``idle`` and ``sleep`` based on the
  daemon's quiet-hours config.

Disabled when ``ALICE_FACE_URL`` is unset OR the empty string. The
fallback URL matches the ``face_state`` CLI on the pi.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, AsyncIterator, Optional

import httpx


log = logging.getLogger(__name__)


# The alice container runs in Docker's default bridge network and can't
# resolve LAN-side mDNS hostnames like ``alice-face.local`` — those work
# from the pi (cronqj@10.20.30.170) where the ``face_state`` CLI lives,
# not from inside this container. Use the ESP32's actual LAN IP so the
# first push lands without a wasted DNS-resolution timeout. Override via
# ``ALICE_FACE_URL`` if the device ever gets a new lease.
DEFAULT_URL = "http://10.20.30.205:8080"
FALLBACK_URL = None
TIMEOUT_SECONDS = 1.0

VALID_STATES = {"idle", "listening", "thinking", "speaking", "happy", "surprised", "sleep"}


class FacePresence:
    """Fire-and-forget state pusher for the alice-face ESP32 LCD.

    Constructed once on daemon startup. ``url`` defaults to
    ``ALICE_FACE_URL`` env var, falling back to ``DEFAULT_URL`` /
    ``FALLBACK_URL`` like the pi-side ``face_state`` CLI. Pass an
    empty string to disable entirely (no HTTP calls, no logs).
    """

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        quiet_hours_fn: Optional[Any] = None,
    ) -> None:
        if url is None:
            url = os.environ.get("ALICE_FACE_URL", DEFAULT_URL)
        self._url = (url or "").rstrip("/")
        self._fallback = FALLBACK_URL if self._url == DEFAULT_URL and FALLBACK_URL else None
        self._quiet_hours_fn = quiet_hours_fn
        self._last_state: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def set_state(self, state: str) -> None:
        """Schedule a POST /state with the given state. Returns immediately."""
        if not self.enabled:
            return
        if state not in VALID_STATES:
            log.warning("face_presence: ignoring unknown state %r", state)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push(state))

    def set_idle(self) -> None:
        """Set ``sleep`` if quiet hours, else ``idle``."""
        if self._quiet_hours_fn is not None:
            try:
                in_quiet = bool(self._quiet_hours_fn())
            except Exception:  # noqa: BLE001
                in_quiet = False
        else:
            in_quiet = False
        self.set_state("sleep" if in_quiet else "idle")

    @contextlib.asynccontextmanager
    async def speaking(self) -> AsyncIterator[None]:
        """Push ``speaking`` on enter, ``thinking`` on exit.

        Used around the outbound dispatch in ``_send_message`` so the
        face shows the speaking glyph for the duration of the actual
        send. The turn continues in ``thinking`` after.
        """
        self.set_state("speaking")
        try:
            yield
        finally:
            self.set_state("thinking")

    async def _push(self, state: str) -> None:
        # Coalesce duplicate consecutive states so we don't spam the
        # face. Holding the lock here serializes overlapping pushes so
        # the last_state check is reliable.
        async with self._lock:
            if state == self._last_state:
                return
            self._last_state = state
            url = self._url
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                    resp = await client.post(
                        f"{url}/state", json={"state": state}
                    )
                    if 200 <= resp.status_code < 300:
                        return
                    log.debug(
                        "face_presence: %s/state returned %d", url, resp.status_code
                    )
            except (httpx.HTTPError, OSError) as e:
                if self._fallback and url != self._fallback:
                    try:
                        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                            await client.post(
                                f"{self._fallback}/state", json={"state": state}
                            )
                            return
                    except (httpx.HTTPError, OSError) as e2:
                        log.debug("face_presence: fallback also failed: %s", e2)
                        return
                log.debug("face_presence: %s unreachable: %s", url, e)


__all__ = ["FacePresence", "DEFAULT_URL", "FALLBACK_URL", "VALID_STATES"]
