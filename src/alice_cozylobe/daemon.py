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
from .adjacency import AdjacencyInferrer
from .breach import AlarmStateCache
from .cortex import DEFAULT_VAULT_ROOT, load_vault
from .event_log import DEFAULT_EVENT_LOG_ROOT, SseEventLogger
from .guesses import GuessLifecycle
from .motion import MotionPipeline
from .qwen_client import DEFAULT_QWEN_ENDPOINT, QwenClient
from .sse_consumer import (
    DEFAULT_EVENTS_URL,
    DEFAULT_QUEUE_SIZE,
    SSEConsumer,
)
from .throttle import (
    DEFAULT_CONFIG_PATH as DEFAULT_THROTTLE_CONFIG_PATH,
    Throttle,
    ensure_user_config,
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
# "<pi-provider>/<model-id>"; the "litellm" provider in the vault's
# pi-models.json points pi at the LiteLLM proxy (alice-litellm:4000),
# and "qwen-desktop" maps to the 3090 desktop (10.20.30.147:8033) in
# sandbox/litellm/config.yaml — so PiKernel reasoning goes through the
# proxy like every other local-model call site, not direct to the box.
DEFAULT_REASONING_MODEL = "litellm/qwen-desktop"

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
        throttle_config_path: pathlib.Path = DEFAULT_THROTTLE_CONFIG_PATH,
        cozylobe_cortex_root: pathlib.Path = DEFAULT_VAULT_ROOT,
        event_log_root: pathlib.Path = DEFAULT_EVENT_LOG_ROOT,
        event_log_enabled: bool = True,
    ) -> None:
        self._events_url = events_url
        self._qwen_endpoint = qwen_endpoint
        self._queue_size = queue_size
        self._cozyhem_base_url = cozyhem_base_url
        self._periodic_cadence_s = periodic_cadence_s
        self._reasoning_model = reasoning_model
        self._reasoning_max_seconds = reasoning_max_seconds
        self._throttle_config_path = pathlib.Path(throttle_config_path)
        self._cozylobe_cortex_root = pathlib.Path(cozylobe_cortex_root)
        self._event_log_root = pathlib.Path(event_log_root)
        self._event_log_enabled = bool(event_log_enabled)
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

        # Seed the user-editable throttle config from the shipped
        # default on first start; subsequent starts find the file
        # already in place and skip. Construct the Throttle pointed at
        # the user-side path so agent edits (#371) are picked up via
        # the per-event mtime check.
        ensure_user_config(user_path=self._throttle_config_path)
        throttle = Throttle(config_path=self._throttle_config_path)

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

        # Phase 2 (#379): load the cozylobe-cortex vault once at boot so
        # the motion pipeline can include the home layout in every
        # classify prompt. load_vault returns an empty Vault if the
        # directory is missing — the daemon stays up on a half-onboarded
        # vault, the prompt just carries an empty cortex snapshot.
        cortex_vault = load_vault(self._cozylobe_cortex_root)
        # Phase 3 (#380) + Phase 4 (#381) wiring: guess lifecycle for
        # self-evident confirmation/refutation, adjacency inferrer for
        # unknown-edge discovery. Both read the same throttle config
        # for their tunable knobs (adjacency window) so operators have
        # a single editable file.
        guess_lifecycle = GuessLifecycle(vault_root=self._cozylobe_cortex_root)
        adjacency_inferrer = AdjacencyInferrer(
            vault=cortex_vault,
            window_s=throttle.config.adjacency_inference_window_s,
        )
        # New trail-based breach classifier (replaces the always-on
        # nighttime actionable trigger that fired five false positives
        # overnight 2026-05-27→28). The cache polls HA every 60s for
        # the alarm-control-panel state; the motion pipeline consults
        # it on every event and runs the four-case trail-shape
        # classifier. See ``breach.py`` for the rules.
        breach_cache = AlarmStateCache()
        motion_pipeline = MotionPipeline(
            qwen_client=qwen,
            vault=cortex_vault,
            vault_root=self._cozylobe_cortex_root,
            lifecycle=guess_lifecycle,
            adjacency_inferrer=adjacency_inferrer,
            breach_cache=breach_cache,
        )

        # Issue #401: raw-event JSONL logger for the NN training corpus.
        # Disabled by passing ``--no-event-log`` (CLI) — the writer
        # short-circuits to a no-op when ``enabled=False`` so a flipped
        # flag doesn't require additional plumbing.
        event_logger = SseEventLogger(
            root=self._event_log_root,
            enabled=self._event_log_enabled,
        )

        wake_loop = WakeLoop(
            emitter=self._emitter,
            qwen_client=qwen,
            agent_spec=cozylobe_spec,
            backend=reasoning_backend,
            fetch_activity=activity_fetcher.fetch,
            periodic_cadence_s=self._periodic_cadence_s,
            throttle=throttle,
            motion_pipeline=motion_pipeline,
            event_logger=event_logger,
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
            throttle_config_path=str(self._throttle_config_path),
            cozylobe_cortex_root=str(self._cozylobe_cortex_root),
            cozylobe_cortex_rooms=len(cortex_vault.rooms),
            cozylobe_cortex_sensors=len(cortex_vault.sensors),
            event_log_root=str(self._event_log_root),
            event_log_enabled=self._event_log_enabled,
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
            # Release the JSONL corpus file descriptor on shutdown.
            # Idempotent + best-effort: a write-failed logger that
            # never opened a file is fine to close.
            event_logger.close()
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
        "--throttle-config",
        default=str(DEFAULT_THROTTLE_CONFIG_PATH),
        help=(
            "Path to the diff-aware-throttle YAML (default: %(default)s). "
            "Agents edit this file at runtime; cozylobe reloads via "
            "mtime check on the next event."
        ),
    )
    parser.add_argument(
        "--cozylobe-cortex-root",
        default=str(DEFAULT_VAULT_ROOT),
        help=(
            "Path to the cozylobe-cortex vault (default: %(default)s). "
            "The motion pipeline (#379 Phase 2) reads this at boot to "
            "ground qwen classify calls in the home's room/sensor "
            "layout."
        ),
    )
    parser.add_argument(
        "--event-log-root",
        default=str(DEFAULT_EVENT_LOG_ROOT),
        help=(
            "Directory for the raw SSE event JSONL corpus (issue #401, "
            "default: %(default)s). Used by a future small sequence "
            "model that replaces qwen's next-room prediction."
        ),
    )
    parser.add_argument(
        "--no-event-log",
        dest="event_log_enabled",
        action="store_false",
        help=(
            "Disable the raw SSE event JSONL logger. Default behavior "
            "is to write one line per INPUT_KINDS event to the per-day "
            "file under --event-log-root. Flip this off if the logger "
            "ever interferes with the motion pipeline."
        ),
    )
    parser.set_defaults(event_log_enabled=True)
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
        throttle_config_path=pathlib.Path(args.throttle_config),
        cozylobe_cortex_root=pathlib.Path(args.cozylobe_cortex_root),
        event_log_root=pathlib.Path(args.event_log_root),
        event_log_enabled=args.event_log_enabled,
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
