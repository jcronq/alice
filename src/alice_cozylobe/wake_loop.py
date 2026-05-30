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
from .event_log import SseEventLogger
from .events import CozyHemEvent
from .motion import MotionPipeline
from .noise_router import (
    BurstCoalescer,
    CoalesceFlush,
    NoiseEvent,
    classify_entity_type,
    coalesce_slug,
    render_coalesced_body,
    should_route_to_noise,
)
from core.llm_client import LLMClient, LLMUnreachable, QwenClassification
from .surfaces import (
    build_slug,
    write_noise_note,
    write_observation_note,
    write_urgent_surface,
)
from .throttle import Throttle


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
        llm_client: Optional[LLMClient] = None,
        agent_spec: Optional[AgentSpec] = None,
        run_agent_fn: Optional[RunAgentCallable] = None,
        backend: object = None,
        fetch_activity: Optional[FetchActivity] = None,
        periodic_cadence_s: float = DEFAULT_PERIODIC_CADENCE_SECONDS,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        throttle: Optional[Throttle] = None,
        motion_pipeline: Optional[MotionPipeline] = None,
        event_logger: Optional[SseEventLogger] = None,
        noise_coalescer: Optional[BurstCoalescer] = None,
        write_note_fn: Optional[Callable[..., object]] = None,
        write_noise_fn: Optional[Callable[..., object]] = None,
    ) -> None:
        self._emitter = emitter
        self._llm = llm_client
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
        # Diff-aware throttle (issue #371). Optional so legacy tests
        # that don't care about throttling can omit it — when None,
        # every event passes through unchanged.
        self._throttle = throttle
        # Phase 2 (#379) motion pipeline. Optional so legacy tests can
        # continue to exercise the generic event path; when None,
        # motion events fall through to the existing classify path.
        self._motion_pipeline = motion_pipeline
        # Issue #401: raw-event JSONL logger for NN training corpus.
        # Optional — None disables logging entirely. The logger sits
        # post-INPUT_KINDS, pre-throttle, pre-motion so OUTPUT events
        # never enter the corpus and duplicate "sensor still on" fires
        # are preserved verbatim (timing information matters for the
        # sequence model).
        self._event_logger = event_logger
        # Issue #411: noise-routing infrastructure. The motion pipeline
        # already coalesces motion events on its 30s window
        # (:class:`MotionQueue`) and routes the resulting batch note
        # through its own writer. For OTHER noise-class entity types
        # (light_level / ambient / humidity) that survive the
        # INPUT_KINDS gate, the backstop note path consults the noise
        # router and either buffers in a 60s coalescer or writes
        # directly to inner/notes/noise/. Both are injection points so
        # tests can spy on the routing without filesystem.
        self._noise_coalescer = noise_coalescer or BurstCoalescer()
        self._write_note_fn = write_note_fn or write_observation_note
        self._write_noise_fn = write_noise_fn or write_noise_note

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
        # Phase 2 (#379): INPUT_KINDS allowlist is the PRIMARY filter,
        # applied BEFORE telemetry, throttle, classify, or any note
        # write. OUTPUT events (circadian brightness updates, propagated
        # light states after a scene change, automation setpoint writes)
        # never enter the pipeline. The throttle config carries the
        # allowlist so agents can edit it at runtime via the same yaml
        # they already edit for the throttle rules.
        if self._throttle is not None and not self._throttle.is_input_kind(event):
            # Single low-cost telemetry event so the drop is visible in
            # the JSONL log; intentionally NO observation note write
            # (the design's "drop silently — output event" branch).
            self._emitter.emit(
                "cozylobe_event_dropped_non_input",
                kind=event.kind,
                entity_id=event.entity_id,
            )
            return

        self._emitter.emit(
            "cozylobe_event_received",
            kind=event.kind,
            entity_id=event.entity_id,
        )

        # Issue #401: append the raw event to the JSONL corpus BEFORE
        # throttle suppression and BEFORE motion routing. Rationale:
        # INPUT_KINDS has already filtered out OUTPUT events (circadian
        # ticks, propagated light states); throttle would collapse
        # duplicate "sensor still on" fires that carry useful inter-
        # event timing information for the future sequence model.
        # Fail-safe inside the logger — never crashes the wake loop.
        if self._event_logger is not None:
            self._event_logger.log(event)

        # Phase 2 (#379): motion is a special class. Motion events
        # BYPASS the throttle entirely and route through the motion
        # pipeline (trail + 30s coalesce queue + qwen classify with
        # cortex snapshot). The throttle's secondary-gate behavior
        # only applies to OTHER input kinds (doorbell, button, door,
        # security, camera, scene, lock).
        if (
            self._motion_pipeline is not None
            and self._motion_pipeline.is_motion_event(event)
        ):
            self._emitter.emit(
                "cozylobe_motion_routed",
                kind=event.kind,
                entity_id=event.entity_id,
            )
            await self._motion_pipeline.handle(event)
            return

        # Diff-aware throttle (#371): suppress / coalesce routine
        # entity:update micro-deltas BEFORE the classify+agent path.
        # CRITICAL kinds are listed in the throttle's always_pass_kinds
        # so doorbell / smoke / glass_break still bypass via the
        # default config; the explicit check below stays as a backstop
        # if the user-edited config drops the override.
        if self._throttle is not None:
            decision = self._throttle.handle(event)
            if decision.action == "drop":
                self._emitter.emit(
                    "cozylobe_event_throttled",
                    kind=event.kind,
                    entity_id=event.entity_id,
                    action="drop",
                    reason=decision.reason,
                )
                return
            if decision.action == "summary":
                self._emitter.emit(
                    "cozylobe_event_throttled",
                    kind=event.kind,
                    entity_id=event.entity_id,
                    action="summary",
                    reason=decision.reason,
                    suppressed_count=decision.event.payload.get(
                        "_suppressed_count", 0
                    ),
                )
                event = decision.event

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

        Issue #411: also drains stale noise-coalescer buffers so a
        light_level burst that stalled below the threshold doesn't
        sit in memory indefinitely. Cheap — at most one note per
        entity type per drained tick.
        """
        # Issue #411: opportunistic stale-flush of the noise coalescer.
        # Runs BEFORE the activity fetch so a fetch-failure short-circuit
        # doesn't skip the drain.
        try:
            self.flush_stale_noise()
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning(
                "cozylobe: noise stale-flush failed during periodic tick: %s",
                exc,
            )

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
        if self._llm is None:
            return None
        try:
            classification = await self._llm.classify(event)
        except LLMUnreachable as exc:
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

        Issue #411 routing: when the event is a low-value sensor type
        (light_level / ambient / humidity per
        :func:`should_route_to_noise`), the backstop note feeds the
        burst coalescer and writes to inner/notes/noise/ — see module
        docstring for the rationale. Motion events have their own
        pipeline and never reach this path (the wake loop branches to
        ``motion_pipeline.handle`` before backstop). Anything not
        classified as noise — light/switch transitions, scenes,
        buttons, locks, doors, windows — continues to land in
        inner/notes/ as before.
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

        if should_route_to_noise(event.entity_id):
            self._route_to_noise(event, body, classification.summary)
            return

        try:
            self._write_note_fn(
                body,
                slug=slug,
                tags=("lobe-observation", "cozylobe-backstop"),
            )
        except OSError as exc:
            log.warning("cozylobe: backstop note write failed: %s", exc)

    def _route_to_noise(
        self,
        event: CozyHemEvent,
        body: str,
        summary: str,
    ) -> None:
        """Push a noise-class event through the burst coalescer.

        Below the threshold → buffered, no immediate write. At the
        threshold → flush a coalesced note covering the window.
        Stale-flush is driven by :meth:`flush_stale_noise` on the
        periodic tick.

        Fails open: if classify_entity_type returns None despite the
        upstream should_route_to_noise check (shouldn't happen), we
        fall back to a direct noise-note write so nothing is silently
        dropped.
        """
        entity_type = classify_entity_type(event.entity_id) or ""
        if not entity_type:
            slug = build_slug(event.kind, event.entity_id, "noise")
            try:
                self._write_noise_fn(
                    body,
                    slug=slug,
                    tags=("lobe-observation", "cozylobe-backstop", "noise"),
                )
            except OSError as exc:
                log.warning(
                    "cozylobe: noise note write failed: %s", exc
                )
            return

        noise_event = NoiseEvent(
            timestamp=event.received_at,
            entity_id=event.entity_id,
            entity_type=entity_type,
            summary=summary or f"kind={event.kind}",
        )
        flush = self._noise_coalescer.add(noise_event)
        self._emitter.emit(
            "cozylobe_noise_buffered",
            entity_id=event.entity_id,
            entity_type=entity_type,
            pending=self._noise_coalescer.pending_count(entity_type),
        )
        if flush is not None:
            self._write_noise_flush(flush)

    def _write_noise_flush(self, flush: CoalesceFlush) -> None:
        """Render a coalesced flush as a single noise note."""
        body = render_coalesced_body(flush)
        slug = coalesce_slug(flush)
        tags = [
            "lobe-observation",
            "noise",
            f"noise-type:{flush.entity_type}",
        ]
        if flush.coalesced:
            tags.append("burst-coalesce")
        else:
            tags.append("noise-stale-flush")
        try:
            self._write_noise_fn(body, slug=slug, tags=tuple(tags))
        except OSError as exc:
            log.warning(
                "cozylobe: noise coalesce write failed: %s", exc
            )
        self._emitter.emit(
            "cozylobe_noise_flushed",
            entity_type=flush.entity_type,
            count=len(flush.events),
            coalesced=flush.coalesced,
        )

    def flush_stale_noise(self) -> None:
        """Drain stale buffers from the noise coalescer.

        Public hook the periodic task calls so events that arrived but
        never crossed the threshold get written before the buffer
        leaks them on a daemon restart. Returns nothing; emits one
        flush event per drained bucket.
        """
        for flush in self._noise_coalescer.flush_stale():
            self._write_noise_flush(flush)

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
