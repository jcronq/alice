"""Long-running loop driving the alice-face status caption.

Module entrypoint::

    /opt/alice-venv/bin/python -m alice_speaking.services.face_caption_loop

Runs forever:

1. Every ``ALICE_FACE_CAPTION_INTERVAL_S`` seconds (default 60), pick
   the latest wake note, ask the LiteLLM proxy's ``qwen-local`` to
   summarize it, and push the summary to the face's ``/status`` endpoint
   via :class:`alice_speaking.infra.face_caption.FaceCaptionDriver`.
2. During quiet hours (23:00–08:00 America/New_York per spec) skip the
   push and just sleep.
3. SIGTERM / SIGINT triggers a graceful exit at the next interval
   boundary.

This loop is independent of the speaking daemon — the s6 supervisor
runs it as its own service. Errors inside :meth:`FaceCaptionDriver.tick`
log and continue; the loop itself never crashes.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import sys
import time
from typing import Optional
from zoneinfo import ZoneInfo

from alice_speaking.infra.face_caption import FaceCaptionDriver


log = logging.getLogger(__name__)


DEFAULT_INTERVAL_SECONDS = 60
QUIET_HOURS_START = dt.time(23, 0)
QUIET_HOURS_END = dt.time(8, 0)
QUIET_HOURS_TZ = ZoneInfo("America/New_York")


def _in_quiet_hours(now: Optional[dt.datetime] = None) -> bool:
    """True if the local clock (America/New_York) is in 23:00–08:00."""
    current = (
        (now or dt.datetime.now(dt.timezone.utc)).astimezone(QUIET_HOURS_TZ).time()
    )
    # Wraps midnight.
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= current < QUIET_HOURS_END
    return current >= QUIET_HOURS_START or current < QUIET_HOURS_END


def _resolve_interval() -> int:
    raw = os.environ.get("ALICE_FACE_CAPTION_INTERVAL_S")
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "face_caption_loop: ALICE_FACE_CAPTION_INTERVAL_S=%r invalid; "
            "falling back to %ds",
            raw,
            DEFAULT_INTERVAL_SECONDS,
        )
        return DEFAULT_INTERVAL_SECONDS
    if value < 5:
        log.warning(
            "face_caption_loop: interval %ds too small; clamping to 5s",
            value,
        )
        return 5
    return value


_stopping = False


def _handle_signal(signum, _frame) -> None:  # noqa: ANN001 — signal handler ABI
    global _stopping
    _stopping = True
    log.info("face_caption_loop: received signal %d, draining", signum)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [face-caption] %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    interval = _resolve_interval()
    driver = FaceCaptionDriver()
    log.info(
        "face_caption_loop: starting; interval=%ds, face_url=%s",
        interval,
        os.environ.get("ALICE_FACE_URL", "<default>"),
    )

    while not _stopping:
        if _in_quiet_hours():
            log.debug("face_caption_loop: quiet hours, skipping tick")
        else:
            pushed = driver.tick()
            if pushed:
                log.info("face_caption_loop: pushed caption: %s", pushed)
        # Sleep in 1s chunks so SIGTERM is responsive.
        slept = 0
        while slept < interval and not _stopping:
            time.sleep(1)
            slept += 1

    log.info("face_caption_loop: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
