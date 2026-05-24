"""Wake loop — push-driven supervisor for the cozylobe.

The loop is dormant between events. When an SSE event arrives:

1. Optionally classify with qwen 27b (fast pattern recognition).
   If qwen is unreachable, log once and skip — never fabricate
   classifications when the model is down (design's
   ``lobe-quiet-on-link-loss`` rule).
2. Dispatch one :func:`core.agent_library.run_agent` call against
   the registered ``cozylobe`` :class:`~core.agent_library.AgentSpec`
   with the event + qwen classification as the user prompt. Per
   Jason's 2026-05-24 directive, the reasoning step MUST go through
   the agent library — direct kernel calls are forbidden.
3. The agent's job is to confirm the analysis and (typically via its
   Write tool) drop an observation note or surface. A deterministic
   surface emitter is also wired here as a backstop for cases the
   agent doesn't reach for the Write tool itself.

Walking-skeleton scope: one event = one agent call = one note. The
30s batching window + urgency-tier fast-path land in follow-up PRs;
the current loop processes events serially as they arrive.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from core.agent_library import AgentSpec, default_registry, run_agent
from core.events import EventEmitter

from .activity_fetcher import ActivitySnapshot, FetchActivity
from .events import CozyHemEvent
from .qwen_client import QwenClassification, QwenClient, QwenUnreachable
from .surfaces import build_slug, write_observation_note, write_urgent_surface


__all__ = ["WakeLoop"]


log = logging.getLogger(__name__)


# Type alias for the run_agent shim so tests can swap in a stub without
# reaching into core.agent_library internals.
RunAgentCallable = Callable[..., Awaitable[object]]


# Default cadence for the periodic wake mode. Five minutes is the
# compromise: short enough that a degrading situation (e.g. anthem
# left on overnight) surfaces in <10min, long enough that the
# reasoning cost stays bounded. Override via the constructor arg or
# the daemon's CLI flag.
DEFAULT_PERIODIC_CADENCE_SECONDS = 300.0


# Kinds we treat as CRITICAL — hardcoded so the lobe responds to
# emergencies even when qwen is unreachable (wake-loop design,
# "CRITICAL fast-path" section). Walking-skeleton scope keeps this
# tight; the full rule set lands with the urgency-tier classifier.
_CRITICAL_KINDS = frozenset(
    {
        "doorbell_pressed",
        "smoke_detected",
        "glass_break",
    }
)


class WakeLoop:
    """Coordinates SSE event arrival → reasoning → surface write.

    Construct once per process. :meth:`run` drains the queue forever
    (or until ``stop`` is set). Dependency-injects the qwen client,
    the run_agent shim, and the surface emitters so tests can verify
    each branch without spinning up real backends.
    """

    def __init__(
        self,
        *,
        emitter: EventEmitter,
        qwen_client: Optional[QwenClient] = None,
        agent_spec: Optional[AgentSpec] = None,
        run_agent_fn: Optional[RunAgentCallable] = None,
        backend: object = None,
        fetch_activity: Optional[FetchActivity] = None,
        periodic_cadence_s: float = DEFAULT_PERIODIC_CADENCE_SECONDS,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._emitter = emitter
        self._qwen = qwen_client
        self._agent_spec = agent_spec or default_registry.get("cozylobe")
        self._run_agent = run_agent_fn or run_agent
        self._backend = backend
        # Track whether we've already logged a qwen-unreachable warning
        # in the current outage. Reset on first successful classify.
        self._qwen_warned = False
        # Periodic-mode wiring. The fetcher is optional so callers that
        # only want the SSE path (e.g. legacy tests) can omit it; the
        # periodic task then no-ops cleanly.
        self._fetch_activity = fetch_activity
        self._periodic_cadence_s = periodic_cadence_s
        self._sleep = sleep or asyncio.sleep

    async def run(
        self,
        queue: "asyncio.Queue[CozyHemEvent]",
        stop: asyncio.Event,
    ) -> None:
        """Drain ``queue`` forever, running one reasoning pass per event.

        ``stop`` short-circuits the wait so daemon shutdown isn't
        blocked by an idle queue. Exceptions in :meth:`_handle_event`
        are logged + swallowed so one bad event can't kill the loop.
        """
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "cozylobe: unhandled error on event kind=%s: %s",
                    event.kind,
                    exc,
                )
                self._emitter.emit(
                    "cozylobe_error",
                    kind=event.kind,
                    entity_id=event.entity_id,
                    error=type(exc).__name__,
                    message=str(exc),
                )

    async def _handle_event(self, event: CozyHemEvent) -> None:
        """One full wake tick for one event."""
        self._emitter.emit(
            "cozylobe_event_received",
            kind=event.kind,
            entity_id=event.entity_id,
        )

        # CRITICAL fast-path: hardcoded rules, skip qwen entirely.
        # Surface immediately so speaking sees doorbell/smoke/glass-break
        # without waiting on the agent dispatch.
        if event.kind in _CRITICAL_KINDS:
            self._surface_critical(event)
            self._emitter.emit(
                "cozylobe_critical_surfaced",
                kind=event.kind,
                entity_id=event.entity_id,
            )
            return

        # Non-critical path: try qwen, then dispatch the agent.
        classification = await self._classify(event)

        # Run the supervisor agent. The agent receives both the raw
        # event and qwen's classification (or a "qwen unreachable"
        # marker) and decides what to write.
        prompt = self._build_agent_prompt(event, classification)
        try:
            await self._run_agent(
                self._agent_spec,
                prompt=prompt,
                emitter=self._emitter,
                backend=self._backend,
                correlation_id=f"cozylobe-{event.kind}-{event.received_at:.0f}",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cozylobe: run_agent failed for kind=%s: %s",
                event.kind,
                exc,
            )
            self._emitter.emit(
                "cozylobe_agent_error",
                kind=event.kind,
                entity_id=event.entity_id,
                error=type(exc).__name__,
            )
            return

        # Backstop note: even if the agent didn't write anything via
        # its Write tool, drop a low-noise observation note so the
        # event leaves a trail. Thinking's drain can dedupe later.
        if classification is not None:
            self._backstop_note(event, classification)

        self._emitter.emit(
            "cozylobe_event_handled",
            kind=event.kind,
            entity_id=event.entity_id,
            urgency=classification.urgency if classification else "UNKNOWN",
            intent=classification.intent if classification else "skipped",
        )

    async def run_periodic(self, stop: asyncio.Event) -> None:
        """Pull-driven audit loop. Every ``periodic_cadence_s`` seconds,
        fetch an activity snapshot and dispatch run_agent with a
        synthetic ``periodic_review`` event.

        Skips the tick (no run_agent dispatch) when the snapshot is
        ``None`` — cozyhem-engine unreachable. This is the periodic-
        mode analog of the qwen ``lobe-quiet-on-link-loss`` rule: do
        not fabricate reasoning input when the substrate is down.

        Cancellation: respects :class:`asyncio.CancelledError` so
        ``daemon.run()``'s shutdown sequence can tear the task down
        cleanly. The interruptible-sleep pattern uses
        :func:`asyncio.wait_for` on ``stop.wait()`` so we don't have
        to wait the full cadence before exiting.
        """
        if self._fetch_activity is None:
            log.info(
                "cozylobe: periodic wake disabled (no activity fetcher "
                "configured)"
            )
            return

        while not stop.is_set():
            try:
                await self._periodic_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # One bad tick must not kill the loop. Log + telemetry +
                # continue. Matches the SSE wake loop's per-event
                # swallow semantics.
                log.exception("cozylobe: periodic tick failed: %s", exc)
                self._emitter.emit(
                    "cozylobe_periodic_error",
                    error=type(exc).__name__,
                    message=str(exc),
                )

            # Interruptible sleep — stop.wait() returns immediately if
            # the daemon is shutting down so we don't have to wait the
            # full cadence on SIGTERM.
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._periodic_cadence_s
                )
            except asyncio.TimeoutError:
                # Normal path — cadence elapsed without stop being set.
                continue
            except asyncio.CancelledError:
                raise

    async def _periodic_tick(self) -> None:
        """One full periodic-mode tick: fetch snapshot → maybe
        dispatch. Pulled out of :meth:`run_periodic` so exceptions
        land in one place and the sleep stays uninterrupted by per-
        tick error handling.
        """
        snapshot = await self._fetch_activity()
        if snapshot is None:
            # Cozyhem unreachable. The fetcher already logged once;
            # we emit telemetry so the skipped tick is visible without
            # adding log noise.
            self._emitter.emit("cozylobe_periodic_skipped_unreachable")
            return

        self._emitter.emit(
            "cozylobe_periodic_tick",
            fetched_at=snapshot.fetched_at,
            partial_errors=len(snapshot.partial_errors),
        )

        prompt = self._build_periodic_prompt(snapshot)
        correlation_id = f"cozylobe-periodic_review-{snapshot.fetched_at:.0f}"
        try:
            await self._run_agent(
                self._agent_spec,
                prompt=prompt,
                emitter=self._emitter,
                backend=self._backend,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cozylobe: run_agent failed for periodic_review: %s", exc
            )
            self._emitter.emit(
                "cozylobe_agent_error",
                kind="periodic_review",
                entity_id="",
                error=type(exc).__name__,
            )
            return

        self._emitter.emit(
            "cozylobe_periodic_handled",
            fetched_at=snapshot.fetched_at,
        )

    def _build_periodic_prompt(self, snapshot: ActivitySnapshot) -> str:
        """Compose the supervisor prompt for the periodic-review path.

        Different framing from the SSE prompt: no specific event to
        triage — instead the lobe is being asked to look at the home's
        current state and decide whether anything is worth surfacing.
        Concise on purpose; the registered AgentSpec carries the
        behavioral rules.
        """
        partial = ""
        if snapshot.partial_errors:
            partial = (
                "\nNOTE: snapshot is partial. The following endpoints "
                f"failed: {'; '.join(snapshot.partial_errors)}\n"
            )
        return (
            "Here's a snapshot of the home's recent activity. Anything "
            "need attention? Drop an observation note "
            "(inner/notes/) if you see a pattern worth remembering, or "
            "an urgent surface (inner/surface/) if action is needed. "
            "Stay quiet if nothing stands out.\n\n"
            f"fetched_at: {snapshot.fetched_at}\n"
            f"entity_states: {snapshot.entity_states}\n"
            f"anthem_status: {snapshot.anthem_status}\n"
            f"lights: {snapshot.lights}\n"
            f"{partial}"
        )

    async def _classify(
        self, event: CozyHemEvent
    ) -> Optional[QwenClassification]:
        """Run the qwen classifier; return ``None`` on unreachable.

        Per the design's ``lobe-quiet-on-link-loss`` rule: log ONCE per
        outage, not on every event. The flag resets on first
        successful classify so a flapping endpoint surfaces every time
        it recovers.
        """
        if self._qwen is None:
            return None
        try:
            classification = await self._qwen.classify(event)
        except QwenUnreachable as exc:
            if not self._qwen_warned:
                log.warning(
                    "cozylobe: qwen unreachable, lobe going quiet on "
                    "reasoning step: %s",
                    exc,
                )
                self._qwen_warned = True
            self._emitter.emit(
                "cozylobe_qwen_unreachable",
                kind=event.kind,
                entity_id=event.entity_id,
            )
            return None
        if self._qwen_warned:
            log.info("cozylobe: qwen recovered")
            self._qwen_warned = False
        return classification

    def _build_agent_prompt(
        self,
        event: CozyHemEvent,
        classification: Optional[QwenClassification],
    ) -> str:
        """Compose the supervisor prompt for ``run_agent``.

        Short on purpose — the registered cozylobe AgentSpec carries
        the behavioral rules (vault-read-only, urgency-via-surface,
        no-direct-cozyhem-mutation, lobe-quiet-on-link-loss) so we
        don't repeat them in the per-event prompt.
        """
        if classification is None:
            classifier_block = (
                "qwen classifier: UNREACHABLE — fall back to logging "
                "this event with no escalation."
            )
        else:
            classifier_block = (
                f"qwen classifier: urgency={classification.urgency} "
                f"intent={classification.intent}\n"
                f"summary: {classification.summary}\n"
                f"reasoning: {classification.reasoning}"
            )
        return (
            "A CozyHem event just arrived. Decide whether to drop an "
            "observation note (inner/notes/) or an urgent surface "
            "(inner/surface/), and write it.\n\n"
            f"event.kind: {event.kind}\n"
            f"event.entity_id: {event.entity_id}\n"
            f"event.payload: {event.payload}\n"
            f"event.received_at: {event.received_at}\n\n"
            f"{classifier_block}\n"
        )

    def _backstop_note(
        self,
        event: CozyHemEvent,
        classification: QwenClassification,
    ) -> None:
        """Deterministic backstop: drop a note so the event leaves a
        trail even if the agent didn't write one. Tagged
        ``cozylobe-backstop`` so thinking can prefer agent-written
        notes during drain.
        """
        slug = build_slug(event.kind, event.entity_id)
        body = (
            f"**kind:** {event.kind}\n"
            f"**entity_id:** {event.entity_id}\n"
            f"**urgency:** {classification.urgency}\n"
            f"**intent:** {classification.intent}\n\n"
            f"{classification.summary}\n\n"
            f"_reasoning: {classification.reasoning}_\n"
        )
        try:
            write_observation_note(
                body,
                slug=slug,
                tags=("lobe-observation", "cozylobe-backstop"),
            )
        except OSError as exc:
            log.warning("cozylobe: backstop note write failed: %s", exc)

    def _surface_critical(self, event: CozyHemEvent) -> None:
        """Drop an urgent surface for hardcoded CRITICAL kinds.

        Skips qwen entirely — speaking gets the alert without waiting
        on the agent dispatch.
        """
        slug = build_slug("critical", event.kind, event.entity_id)
        body = (
            f"CRITICAL event: **{event.kind}**\n"
            f"entity_id: `{event.entity_id}`\n"
            f"payload: `{event.payload}`\n\n"
            "Hardcoded fast-path (no qwen, no agent dispatch). "
            "Speaking should surface this to Jason immediately.\n"
        )
        try:
            write_urgent_surface(
                body,
                slug=slug,
                extra_frontmatter={
                    "urgency": "CRITICAL",
                    "event_kind": event.kind,
                    "entity_id": event.entity_id,
                },
            )
        except OSError as exc:
            log.error(
                "cozylobe: CRITICAL surface write failed for kind=%s: %s",
                event.kind,
                exc,
            )
