"""Cozylobe daemon — supervises the SSE consumer + wake loop.

Long-running process: opens one SSE connection to cozyhem-engine,
runs the :class:`WakeLoop` against the resulting event queue, exits
cleanly on SIGTERM. Intended to run as an s6 service inside the
alice container, alongside the speaking daemon and the thinking
cron. Service-unit wiring lands in a follow-up PR — for the walking
skeleton this module is invokable directly with
``python -m alice_cozylobe``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import signal
import sys
from dataclasses import replace
from typing import Optional

from core.agent_library import default_registry
from core.config.model import BackendSpec
from core.events import EventLogger

from .activity_fetcher import (
    DEFAULT_COZYHEM_BASE_URL,
    ActivityFetcher,
)
from .qwen_client import DEFAULT_QWEN_ENDPOINT, QwenClient
from .sse_consumer import (
    DEFAULT_EVENTS_URL,
    DEFAULT_QUEUE_SIZE,
    SSEConsumer,
)
from .wake_loop import DEFAULT_PERIODIC_CADENCE_SECONDS, WakeLoop


__all__ = ["CozylobeDaemon", "main"]


log = logging.getLogger(__name__)


DEFAULT_LOG = pathlib.Path("/state/worker/cozylobe.log")

# Cozylobe's reasoning step runs on the LOCAL qwen model on the 3090
# desktop, driven through pi-coding-agent (PiKernel) — never Anthropic.
# The walking skeleton constructed the WakeLoop without a backend, so
# run_agent fell through to its BackendSpec(backend="subscription")
# default and dispatched claude-opus-4-7 on every single SSE event.
# This pins the lobe to pi+qwen instead. The model string is
# "<pi-provider>/<model-id>"; the provider's endpoint is declared in
# the vault's pi-models.json (openai-desktop -> 10.20.30.147:8033).
DEFAULT_REASONING_MODEL = "openai-desktop/qwen3.6-27b"

# Bound each reasoning pass. The registered cozylobe template uses
# max_seconds=0 (unbounded); a slow/hung qwen on every event would
# otherwise wedge the wake loop. 180s is generous for a 27B local
# triage pass.
DEFAULT_REASONING_MAX_SECONDS = 180


class CozylobeDaemon:
    """Owns the SSE consumer + wake loop tasks for one process lifetime.

    :meth:`run` is the long-poll: start both tasks, wait for either to
    exit or for ``stop`` to be set, then cancel the survivor and
    return. Crash semantics: an exception in either task surfaces as
    a ``cozylobe_task_died`` event and triggers shutdown so an
    external supervisor (s6) can restart us with a fresh state.
    """

    def __init__(
        self,
        *,
        events_url: str = DEFAULT_EVENTS_URL,
        qwen_endpoint: Optional[str] = DEFAULT_QWEN_ENDPOINT,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        log_path: pathlib.Path = DEFAULT_LOG,
        cozyhem_base_url: str = DEFAULT_COZYHEM_BASE_URL,
        periodic_cadence_s: float = DEFAULT_PERIODIC_CADENCE_SECONDS,
        reasoning_model: str = DEFAULT_REASONING_MODEL,
        reasoning_max_seconds: int = DEFAULT_REASONING_MAX_SECONDS,
    ) -> None:
        self._events_url = events_url
        self._qwen_endpoint = qwen_endpoint
        self._queue_size = queue_size
        self._cozyhem_base_url = cozyhem_base_url
        self._periodic_cadence_s = periodic_cadence_s
        self._reasoning_model = reasoning_model
        self._reasoning_max_seconds = reasoning_max_seconds
        self._emitter = EventLogger(log_path)
        self._stop = asyncio.Event()

    async def run(self) -> int:
        """Long-running event loop. Returns the would-be process exit
        code so callers can exit on it directly.

        Supervises three tasks independently:

        * ``cozylobe-sse`` — long-lived SSE consumer feeding the queue.
        * ``cozylobe-wake`` — push-driven event handler (drains queue).
        * ``cozylobe-periodic`` — pull-driven periodic audit. Fetches
          a state snapshot every ``periodic_cadence_s`` seconds and
          dispatches a synthetic ``periodic_review`` event so the
          lobe reasons about the home even when SSE is quiet.

        Crash semantics: any task exiting causes the daemon to shut
        down (s6 then restarts the process). The two-tier supervision
        from the walking skeleton holds — one task dying triggers
        ``self._stop`` and cancels the others.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)

        qwen = QwenClient(self._qwen_endpoint) if self._qwen_endpoint else None
        consumer = SSEConsumer(self._events_url)
        activity_fetcher = ActivityFetcher(self._cozyhem_base_url)

        # Route the reasoning step through pi-coding-agent + local qwen,
        # NOT the Anthropic subscription default. We override two things
        # on the registered cozylobe spec: the backend (pi-mono -> PiKernel)
        # and the model (a pi "provider/model" string). build_spec() carries
        # the model into the KernelSpec PiKernel dispatches; the backend is
        # what make_kernel() switches on. Both must change together — a pi
        # backend with a claude-* model, or a qwen model with the default
        # backend, would each be wrong.
        reasoning_backend = BackendSpec(
            backend="pi",
            harness="pi-mono",
            model=self._reasoning_model,
        )
        base_spec = default_registry.get("cozylobe")
        cozylobe_spec = replace(
            base_spec,
            runtime="pi",
            kernel_spec=replace(
                base_spec.kernel_spec,
                model=self._reasoning_model,
                max_seconds=self._reasoning_max_seconds,
            ),
        )

        wake_loop = WakeLoop(
            emitter=self._emitter,
            qwen_client=qwen,
            agent_spec=cozylobe_spec,
            backend=reasoning_backend,
            fetch_activity=activity_fetcher.fetch,
            periodic_cadence_s=self._periodic_cadence_s,
        )

        sse_task = asyncio.create_task(
            consumer.run(queue, self._stop), name="cozylobe-sse"
        )
        loop_task = asyncio.create_task(
            wake_loop.run(queue, self._stop), name="cozylobe-wake"
        )
        periodic_task = asyncio.create_task(
            wake_loop.run_periodic(self._stop), name="cozylobe-periodic"
        )

        self._emitter.emit(
            "cozylobe_daemon_started",
            events_url=self._events_url,
            qwen_endpoint=self._qwen_endpoint or "",
            queue_size=self._queue_size,
            cozyhem_base_url=self._cozyhem_base_url,
            periodic_cadence_s=self._periodic_cadence_s,
            reasoning_backend="pi",
            reasoning_model=self._reasoning_model,
        )

        supervised = {sse_task, loop_task, periodic_task}
        try:
            done, pending = await asyncio.wait(
                supervised,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    log.exception(
                        "cozylobe: task %s died: %s",
                        task.get_name(),
                        exc,
                    )
                    self._emitter.emit(
                        "cozylobe_task_died",
                        task=task.get_name(),
                        error=type(exc).__name__,
                        message=str(exc),
                    )
            self._stop.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._emitter.emit("cozylobe_daemon_stopped")

        return 0

    def request_stop(self) -> None:
        """Signal the daemon to exit at the next loop tick. Installed
        on SIGTERM / SIGINT by :func:`main`."""
        self._stop.set()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cozylobe daemon — SSE consumer + push-driven wake loop for "
            "the CozyHem reasoning lobe."
        )
    )
    parser.add_argument(
        "--events-url",
        default=DEFAULT_EVENTS_URL,
        help="CozyHem SSE events URL (default: %(default)s)",
    )
    parser.add_argument(
        "--qwen-endpoint",
        default=DEFAULT_QWEN_ENDPOINT,
        help=(
            "Qwen 27b OpenAI-compatible endpoint. Pass empty string to "
            "disable qwen (lobe stays quiet on reasoning, agent still "
            "runs)."
        ),
    )
    parser.add_argument(
        "--queue-size",
        type=int,
        default=DEFAULT_QUEUE_SIZE,
        help="SSE event queue depth (default: %(default)s)",
    )
    parser.add_argument(
        "--log",
        default=str(DEFAULT_LOG),
        help="JSONL event log path (default: %(default)s)",
    )
    parser.add_argument(
        "--cozyhem-base-url",
        default=DEFAULT_COZYHEM_BASE_URL,
        help=(
            "CozyHem REST base URL for the periodic activity fetcher "
            "(default: %(default)s). Derived from --events-url's host "
            "by default; pass explicitly to point at a different host."
        ),
    )
    parser.add_argument(
        "--periodic-cadence-s",
        type=float,
        default=DEFAULT_PERIODIC_CADENCE_SECONDS,
        help=(
            "Seconds between periodic-review wakes "
            "(default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--reasoning-model",
        default=DEFAULT_REASONING_MODEL,
        help=(
            "pi 'provider/model' string for the reasoning step "
            "(default: %(default)s). Provider endpoint is declared in "
            "the vault's pi-models.json. Cozylobe reasons on local qwen "
            "via pi-coding-agent — it must never call Anthropic."
        ),
    )
    parser.add_argument(
        "--reasoning-max-seconds",
        type=int,
        default=DEFAULT_REASONING_MAX_SECONDS,
        help="Per-event reasoning timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable INFO-level Python logging to stderr.",
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    daemon = CozylobeDaemon(
        events_url=args.events_url,
        qwen_endpoint=args.qwen_endpoint or None,
        queue_size=args.queue_size,
        log_path=pathlib.Path(args.log),
        cozyhem_base_url=args.cozyhem_base_url,
        periodic_cadence_s=args.periodic_cadence_s,
        reasoning_model=args.reasoning_model,
        reasoning_max_seconds=args.reasoning_max_seconds,
    )

    loop = asyncio.new_event_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, daemon.request_stop)
            except (NotImplementedError, RuntimeError):
                # Signal handlers aren't available on every platform
                # (Windows, certain embedded runtimes). The daemon
                # still exits cleanly via KeyboardInterrupt below.
                pass
        return loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        daemon.request_stop()
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
