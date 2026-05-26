"""Motion pipeline — Phase 2 of cozylobe motion-cortex (#379).

Motion events are a distinct class in cozylobe, NOT generic
``entity:update``. The motion-cortex design (cortex-memory/research/
2026-05-26-cozylobe-motion-cortex.md §1.2 + §3) says every motion event
triggers the motion pipeline:

1. Append to a sliding window of the last N motion events
   (:class:`MotionTrail`, default 24).
2. Enqueue into a 30-second coalesce queue
   (:class:`MotionQueue`). Multiple firings within the window are
   processed as one batch — token cost is not a concern, latency is.
3. On flush, assemble a qwen prompt with (a) the current batch,
   (b) the trail snapshot, (c) a cozylobe-cortex snapshot
   (rooms + sensors + adjacency.graph) and call qwen via
   :class:`alice_cozylobe.qwen_client.QwenClient`.
4. Write the positional inference to ``inner/notes/`` tagged
   ``motion-pipeline``. No cortex writes yet — that's Phase 3.

Security-class events (motion in an unexpected room at unexpected
hours — see design §4.5) bypass the queue and classify immediately.
Phase 2 uses a simple time-window heuristic; the full predicate
(active guesses + time-of-day priors) lands with the guess
lifecycle in Phase 3.

The module is sandbox-only: it reads the cozylobe-cortex vault but
writes only to ``inner/notes/`` via
:func:`alice_cozylobe.surfaces.write_observation_note`. No direct
cortex writes, no MCP calls, no shell outs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional

from . import cortex as cortex_mod
from .events import CozyHemEvent
from .qwen_client import QwenClient, QwenUnreachable
from .surfaces import build_slug, write_observation_note


__all__ = [
    "DEFAULT_BATCH_WINDOW_S",
    "DEFAULT_MAX_BATCH_SIZE",
    "DEFAULT_SECURITY_NIGHT_START_HOUR",
    "DEFAULT_SECURITY_NIGHT_END_HOUR",
    "DEFAULT_TRAIL_SIZE",
    "MOTION_EVENT_KINDS",
    "MotionEvent",
    "MotionInference",
    "MotionPipeline",
    "MotionQueue",
    "MotionTrail",
    "build_motion_prompt",
    "classify_motion_batch",
    "is_security_class",
]


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants

# Trail size: the rolling window of recent motion events the classify
# call sees as context. Design §1.2 / §3.1 calls for 12-24; we default
# to the upper bound since 24 events still fits in a single qwen prompt
# comfortably and gives the classifier more trajectory context.
DEFAULT_TRAIL_SIZE = 24

# Coalesce window: the batch flushes when this many seconds elapse
# since the first event in the current batch. Design §3.3 + §4.4.
DEFAULT_BATCH_WINDOW_S = 30.0

# Burst protection: even within the window, force a flush after this
# many events. Avoids unbounded memory on a sensor that's stuck in a
# fast retrigger loop. 50 events in 30s is well above normal human
# motion patterns; if we hit it, something's off and we want to surface
# the batch immediately.
DEFAULT_MAX_BATCH_SIZE = 50

# Security-class heuristic (Phase 2 simplification). Motion firing
# during these hours (local time) is treated as security-class and
# bypasses the queue. Phase 3 will refine this with active-guess
# context and per-room expected-occupancy priors. Hours are
# inclusive-start, inclusive-end across midnight: [23, 0..7) means
# 23:00–06:59 inclusive.
DEFAULT_SECURITY_NIGHT_START_HOUR = 23
DEFAULT_SECURITY_NIGHT_END_HOUR = 7

# Event kinds we treat as "motion" for the special-class flow. The SSE
# producer emits ``motion_detected`` for the canonical Hue/CozyHem
# event; we keep the set extensible for future producers without
# rewriting the wake_loop routing.
MOTION_EVENT_KINDS: frozenset[str] = frozenset({"motion_detected"})


# ---------------------------------------------------------------------------
# Dataclasses


@dataclass(frozen=True)
class MotionEvent:
    """One motion-sensor firing, enriched with the resolved room.

    The wake loop converts raw :class:`CozyHemEvent` records into this
    shape before they hit the trail or the queue: it does the
    sensor → room lookup against the cortex vault once, so downstream
    code doesn't re-resolve on every classify call.
    """

    timestamp: float
    entity_id: str
    state: str
    room_id: Optional[str]

    @classmethod
    def from_cozyhem(
        cls,
        event: CozyHemEvent,
        *,
        vault: Optional[cortex_mod.Vault] = None,
    ) -> "MotionEvent":
        """Build a :class:`MotionEvent` from a raw SSE event.

        Resolves the sensor's room via :func:`cortex.sensor_room` if a
        vault is available; returns ``room_id=None`` otherwise so
        downstream code can still operate on a half-onboarded vault.
        ``state`` is pulled from the payload's ``state`` field; missing
        → empty string (caller decides whether to surface that).
        """
        state_raw = event.payload.get("state", "")
        state = str(state_raw) if state_raw is not None else ""
        room_id: Optional[str] = None
        if vault is not None and event.entity_id:
            room = cortex_mod.sensor_room(vault, event.entity_id)
            if room is not None:
                room_id = room.title
        return cls(
            timestamp=event.received_at,
            entity_id=event.entity_id,
            state=state,
            room_id=room_id,
        )


@dataclass(frozen=True)
class MotionInference:
    """Structured positional inference returned by classify_motion_batch.

    Mirrors the design's §3.2 ``POSITIONAL_INFERENCE`` block. Fields
    that qwen omits become ``None`` / defaults so downstream code can
    operate on a partial response without crashing — graceful degrade
    in line with the existing :class:`QwenClassification` pattern.
    """

    current_room: Optional[str]
    confidence: Optional[float]
    person_hypothesis: Optional[str]
    person_confidence: Optional[float]
    next_room_hypothesis: Optional[str]
    next_room_confidence: Optional[float]
    reasoning: str
    raw: dict


# ---------------------------------------------------------------------------
# Trail


class MotionTrail:
    """Sliding window of the last N motion events.

    Deque-backed so append and trim are O(1). The trail is read by the
    classify call (snapshot copy) and by the security-class heuristic
    (no copy needed). Single-consumer use; no locking.
    """

    def __init__(self, max_size: int = DEFAULT_TRAIL_SIZE) -> None:
        if max_size <= 0:
            raise ValueError(f"max_size must be positive, got {max_size}")
        self._max_size = max_size
        self._events: deque[MotionEvent] = deque(maxlen=max_size)

    def append(self, event: MotionEvent) -> None:
        """Push one event onto the trail; oldest is auto-evicted when
        the deque is at capacity."""
        self._events.append(event)

    def snapshot(self) -> tuple[MotionEvent, ...]:
        """Return an immutable copy of the trail in oldest-first order.

        Used as input to the classify prompt. We materialize a tuple
        rather than expose the deque so callers can't mutate the trail
        between snapshot and classify.
        """
        return tuple(self._events)

    @property
    def max_size(self) -> int:
        return self._max_size

    def __len__(self) -> int:
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()


# ---------------------------------------------------------------------------
# Queue


class MotionQueue:
    """30-second coalesce queue for batched motion events.

    Events accumulate via :meth:`add` until either (a) the configured
    ``batch_window_s`` elapses since the first event in the current
    batch, or (b) ``max_batch_size`` is reached. The queue does not
    own the flush — it's a state holder. The caller (typically
    :class:`MotionPipeline`) polls :meth:`is_ready` / :meth:`take_batch`
    after each add and on a periodic timer so flushes happen even when
    no new events arrive.

    Single-consumer; no locking.
    """

    def __init__(
        self,
        *,
        batch_window_s: float = DEFAULT_BATCH_WINDOW_S,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if batch_window_s <= 0:
            raise ValueError(
                f"batch_window_s must be positive, got {batch_window_s}"
            )
        if max_batch_size <= 0:
            raise ValueError(
                f"max_batch_size must be positive, got {max_batch_size}"
            )
        self._batch_window_s = batch_window_s
        self._max_batch_size = max_batch_size
        self._clock = clock or time.time
        self._batch: list[MotionEvent] = []
        # Wall-clock time of the first event in the current batch. Used
        # to decide when the coalesce window has elapsed. None means
        # the batch is empty.
        self._batch_started_at: Optional[float] = None

    @property
    def batch_window_s(self) -> float:
        return self._batch_window_s

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    def add(self, event: MotionEvent) -> None:
        """Append one event to the current batch.

        Sets the batch-start timestamp if this is the first event in a
        fresh batch (so the coalesce window starts ticking from the
        first event, not from queue construction).
        """
        if not self._batch:
            self._batch_started_at = self._clock()
        self._batch.append(event)

    def is_ready(self) -> bool:
        """True iff the batch is non-empty AND either the window has
        elapsed or ``max_batch_size`` is reached.

        Caller calls this after every :meth:`add` and on a periodic
        timer (so empty bursts still flush). On True, caller should
        :meth:`take_batch` and run the classify call.
        """
        if not self._batch:
            return False
        if len(self._batch) >= self._max_batch_size:
            return True
        assert self._batch_started_at is not None  # invariant
        elapsed = self._clock() - self._batch_started_at
        return elapsed >= self._batch_window_s

    def take_batch(self) -> list[MotionEvent]:
        """Drain the current batch and reset the queue. Returns the
        events in the order they were added; empty list if the queue
        was empty.
        """
        batch = self._batch
        self._batch = []
        self._batch_started_at = None
        return batch

    def __len__(self) -> int:
        return len(self._batch)


# ---------------------------------------------------------------------------
# Security-class predicate


def is_security_class(
    event: MotionEvent,
    *,
    night_start_hour: int = DEFAULT_SECURITY_NIGHT_START_HOUR,
    night_end_hour: int = DEFAULT_SECURITY_NIGHT_END_HOUR,
    localtime: Optional[Callable[[float], time.struct_time]] = None,
) -> bool:
    """Phase 2 security-class heuristic.

    Returns True when motion fires during the nighttime window (default
    23:00–06:59 local). Phase 3 will replace this with the full design
    §4.5 predicate: motion in a room with no one expected, given the
    active guess set + time-of-day priors. For Phase 2 we lean on a
    simple time gate so the bypass path is testable end-to-end.

    The ``localtime`` hook lets tests inject a deterministic struct
    rather than depend on wall-clock at run time.
    """
    lt = localtime or time.localtime
    hour = lt(event.timestamp).tm_hour
    if night_start_hour <= night_end_hour:
        # Window doesn't cross midnight.
        return night_start_hour <= hour < night_end_hour
    # Crosses midnight: e.g. [23, 7) → 23, 0, 1, ..., 6.
    return hour >= night_start_hour or hour < night_end_hour


# ---------------------------------------------------------------------------
# Prompt assembly + classify


def build_motion_prompt(
    batch: Iterable[MotionEvent],
    trail: Iterable[MotionEvent],
    vault: Optional[cortex_mod.Vault],
) -> str:
    """Assemble the qwen prompt for one motion-batch classify call.

    The prompt has three sections, matching design §3.1:

    * CURRENT_BATCH — the events being classified (the latest 30s of
      activity).
    * MOTION_TRAIL — up to 24 recent events for trajectory context.
    * CORTEX_STATE — rooms, sensors, and adjacency snapshot from the
      cozylobe-cortex vault. Keeps qwen grounded in the actual home
      layout instead of hallucinating room names.

    Returns a JSON-output-only prompt that asks qwen for the
    positional inference shape. The classify call parses the response
    with :func:`classify_motion_batch`.
    """
    batch_list = list(batch)
    trail_list = list(trail)
    batch_summary = [_serialize_motion(e) for e in batch_list]
    trail_summary = [_serialize_motion(e) for e in trail_list]
    cortex_snapshot = _serialize_cortex(vault)

    return (
        "You are a positional-inference engine for a smart home. Given "
        "the current batch of motion events, the recent motion trail, "
        "and the home's room/sensor layout, infer where the occupant "
        "is and where they might be heading.\n\n"
        f"CURRENT_BATCH (last ~30s):\n{json.dumps(batch_summary, separators=(',', ':'))}\n\n"
        f"MOTION_TRAIL (last {len(trail_list)} events, oldest first):\n"
        f"{json.dumps(trail_summary, separators=(',', ':'))}\n\n"
        f"CORTEX_STATE:\n{json.dumps(cortex_snapshot, separators=(',', ':'))}\n\n"
        "RULES:\n"
        "1. Reason about WHICH room the person is in now, given the "
        "latest events in the batch. Empty batch = no current motion.\n"
        "2. Reason about WHERE THEY ARE GOING using the trail and the "
        "adjacency graph. Don't propose rooms that aren't adjacent.\n"
        "3. Person identity (Jason / Katie / unknown) is a hypothesis; "
        "low confidence is fine and preferred over a confident guess.\n"
        "4. Keep confidence in [0.0, 1.0]. Be conservative.\n"
        "5. If multiple sensors fired concurrently in non-adjacent "
        "rooms, that's evidence of multiple people OR sensor noise — "
        "say so in `reasoning`.\n\n"
        "RETURN ONLY a JSON object with this structure:\n"
        "{\n"
        "  \"current_room\": \"<room title>\" | null,\n"
        "  \"confidence\": <float in [0,1]>,\n"
        "  \"person_hypothesis\": \"<name>\" | null,\n"
        "  \"person_confidence\": <float in [0,1]>,\n"
        "  \"next_room_hypothesis\": \"<room title>\" | null,\n"
        "  \"next_room_confidence\": <float in [0,1]>,\n"
        "  \"reasoning\": \"max 40 words\"\n"
        "}\n\n"
        "No preamble. No markdown. No explanation outside the JSON object."
    )


def _serialize_motion(event: MotionEvent) -> dict:
    """Render a MotionEvent as a small JSON-safe dict for the prompt."""
    return {
        "ts": event.timestamp,
        "entity_id": event.entity_id,
        "state": event.state,
        "room": event.room_id,
    }


def _serialize_cortex(vault: Optional[cortex_mod.Vault]) -> dict:
    """Render a compact cortex snapshot — rooms, sensors, adjacency.

    Skips note bodies and frontmatter we don't need; qwen only needs
    enough to ground room names + sensor → room mappings + the
    adjacency graph for next-room reasoning. Returns an empty snapshot
    when the vault is missing or unloaded.
    """
    if vault is None:
        return {"rooms": [], "sensors": [], "adjacency": {}}

    rooms = sorted(r.title for r in vault.rooms.values())
    sensors = []
    for s in vault.sensors.values():
        sensors.append(
            {
                "entity_id": s.title,
                "room": (s.room.split("/", 1)[-1] if s.room else None),
            }
        )
    # Adjacency from each room's frontmatter `adjacent:` field. The
    # adjacency.graph JSON file is the machine-readable mirror, but
    # for the prompt we render the in-memory view so a half-onboarded
    # vault still produces usable context.
    adjacency: dict[str, list[str]] = {}
    for room in vault.rooms.values():
        targets = []
        for adj in room.adjacent:
            # adjacent values can be "rooms/Kitchen" or "Kitchen".
            targets.append(adj.split("/", 1)[-1])
        if targets:
            adjacency[room.title] = sorted(set(targets))
    return {
        "rooms": rooms,
        "sensors": sorted(sensors, key=lambda s: s["entity_id"]),
        "adjacency": adjacency,
    }


async def classify_motion_batch(
    batch: list[MotionEvent],
    trail: Iterable[MotionEvent],
    *,
    qwen_client: QwenClient,
    vault: Optional[cortex_mod.Vault] = None,
) -> MotionInference:
    """Classify one motion batch via local qwen.

    Returns a :class:`MotionInference` with the positional-inference
    fields parsed out of qwen's JSON response. Raises
    :class:`QwenUnreachable` on network or parse failure — the caller
    (the pipeline) catches and degrades gracefully.
    """
    prompt = build_motion_prompt(batch, trail, vault)
    parsed = await qwen_client.complete(prompt)
    return MotionInference(
        current_room=_opt_str(parsed.get("current_room")),
        confidence=_opt_float(parsed.get("confidence")),
        person_hypothesis=_opt_str(parsed.get("person_hypothesis")),
        person_confidence=_opt_float(parsed.get("person_confidence")),
        next_room_hypothesis=_opt_str(parsed.get("next_room_hypothesis")),
        next_room_confidence=_opt_float(parsed.get("next_room_confidence")),
        reasoning=str(parsed.get("reasoning", "")),
        raw=parsed,
    )


def _opt_str(value: object) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _opt_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pipeline


class MotionPipeline:
    """High-level glue: trail + queue + classify + note write.

    The wake loop holds one instance per daemon process. On each
    motion event the wake loop calls :meth:`handle`; the pipeline
    appends to the trail, decides between security-class fast-path
    and the normal coalesce queue, and (when ready) runs the classify
    call + writes the observation note.

    The pipeline does NOT modify cozylobe-cortex — that's Phase 3.
    All output goes through :func:`write_observation_note` so thinking's
    drain sees it as a regular fleeting note tagged
    ``motion-pipeline``.
    """

    def __init__(
        self,
        *,
        qwen_client: Optional[QwenClient],
        vault: Optional[cortex_mod.Vault] = None,
        trail: Optional[MotionTrail] = None,
        queue: Optional[MotionQueue] = None,
        write_note: Optional[
            Callable[..., object]
        ] = None,
        clock: Optional[Callable[[], float]] = None,
        localtime: Optional[Callable[[float], time.struct_time]] = None,
        security_predicate: Optional[
            Callable[[MotionEvent], bool]
        ] = None,
    ) -> None:
        self._qwen = qwen_client
        self._vault = vault
        self._trail = trail or MotionTrail()
        self._queue = queue or MotionQueue(clock=clock)
        self._write_note = write_note or write_observation_note
        self._clock = clock or time.time
        self._localtime = localtime or time.localtime
        self._security_predicate = security_predicate
        # Lock-free single-consumer use: the wake loop drains one
        # event at a time on one asyncio task.

    @property
    def trail(self) -> MotionTrail:
        return self._trail

    @property
    def queue(self) -> MotionQueue:
        return self._queue

    @staticmethod
    def is_motion_event(event: CozyHemEvent) -> bool:
        """Return True iff ``event.kind`` is one of the motion kinds.

        Centralized here so wake_loop doesn't duplicate the set.
        """
        return event.kind in MOTION_EVENT_KINDS

    async def handle(self, event: CozyHemEvent) -> None:
        """Process one motion event through the full pipeline.

        Steps:

        1. Build a :class:`MotionEvent` (sensor → room lookup via the
           cortex vault).
        2. Append to the trail (always, even on security-class).
        3. If security-class, classify + write the note IMMEDIATELY,
           bypassing the queue.
        4. Otherwise, add to the queue and flush if the window has
           elapsed or the batch is full.

        Exceptions in the classify or note write are logged but do
        NOT propagate — the wake loop's per-event error handling sees
        them as warnings, not as a reason to kill the loop. Same
        pattern as the existing _classify graceful-degrade.
        """
        motion = MotionEvent.from_cozyhem(event, vault=self._vault)
        self._trail.append(motion)

        if self._is_security_class(motion):
            await self._classify_and_write([motion], security=True)
            return

        self._queue.add(motion)
        if self._queue.is_ready():
            batch = self._queue.take_batch()
            await self._classify_and_write(batch, security=False)

    async def flush_if_ready(self) -> None:
        """Public hook for a periodic timer in wake_loop. Drains the
        queue if the window has elapsed even when no new event has
        arrived. The wake_loop's existing periodic timer can call this
        once per cadence tick — out of scope for Phase 2 wiring but
        kept here so Phase 3 doesn't need to widen the surface.
        """
        if self._queue.is_ready():
            batch = self._queue.take_batch()
            await self._classify_and_write(batch, security=False)

    def _is_security_class(self, motion: MotionEvent) -> bool:
        if self._security_predicate is not None:
            return self._security_predicate(motion)
        return is_security_class(motion, localtime=self._localtime)

    async def _classify_and_write(
        self, batch: list[MotionEvent], *, security: bool
    ) -> None:
        """Internal: run the qwen classify call and write the note.

        Logs + swallows :class:`QwenUnreachable` so an offline classifier
        doesn't take the motion pipeline down. Logs + swallows
        :class:`OSError` on the note write for the same reason.
        """
        if not batch:
            return
        if self._qwen is None:
            # No classifier wired — drop a degraded note so the event
            # still leaves a trail. Matches the design's "log-only on
            # link loss" rule.
            self._write_degraded_note(batch, security=security, reason="qwen_disabled")
            return
        try:
            inference = await classify_motion_batch(
                batch,
                self._trail.snapshot(),
                qwen_client=self._qwen,
                vault=self._vault,
            )
        except QwenUnreachable as exc:
            log.warning(
                "cozylobe motion: qwen unreachable (batch=%d security=%s): %s",
                len(batch),
                security,
                exc,
            )
            self._write_degraded_note(
                batch, security=security, reason=f"qwen_unreachable: {exc}"
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "cozylobe motion: classify failed (batch=%d security=%s): %s",
                len(batch),
                security,
                exc,
            )
            self._write_degraded_note(
                batch,
                security=security,
                reason=f"classify_error: {type(exc).__name__}",
            )
            return
        self._write_inference_note(batch, inference, security=security)

    def _write_inference_note(
        self,
        batch: list[MotionEvent],
        inference: MotionInference,
        *,
        security: bool,
    ) -> None:
        """Render the inference as a fleeting note in inner/notes/.

        Tagged ``motion-pipeline`` so thinking can spot the new Phase 2
        flow during drain (plus ``lobe-observation`` for compatibility
        with the existing drain rules, and ``motion-security`` when the
        security fast-path fired).
        """
        slug_parts = ["motion", "batch", str(len(batch))]
        if inference.current_room:
            slug_parts.append(inference.current_room)
        slug = build_slug(*slug_parts)

        tags = ["lobe-observation", "motion-pipeline"]
        if security:
            tags.append("motion-security")

        body = self._render_body(batch, inference, security=security)
        try:
            self._write_note(body, slug=slug, tags=tuple(tags))
        except OSError as exc:
            log.warning("cozylobe motion: note write failed: %s", exc)

    def _write_degraded_note(
        self,
        batch: list[MotionEvent],
        *,
        security: bool,
        reason: str,
    ) -> None:
        """Drop a minimal note when the classify call couldn't run.

        Keeps a trail in inner/notes/ so the event isn't silently
        dropped during a qwen outage; tagged with ``motion-pipeline``
        + ``motion-degraded`` so drain can dedupe.
        """
        slug = build_slug("motion", "batch", str(len(batch)), "degraded")
        tags = ["lobe-observation", "motion-pipeline", "motion-degraded"]
        if security:
            tags.append("motion-security")
        body = (
            f"**motion batch ({len(batch)} event(s), "
            f"security={'yes' if security else 'no'})** — "
            f"classify skipped: {reason}\n\n"
            + self._render_batch_lines(batch)
        )
        try:
            self._write_note(body, slug=slug, tags=tuple(tags))
        except OSError as exc:
            log.warning("cozylobe motion: degraded note write failed: %s", exc)

    @staticmethod
    def _render_batch_lines(batch: list[MotionEvent]) -> str:
        """Human-readable bullet list of the events in the batch."""
        lines = []
        for e in batch:
            ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(e.timestamp))
            room = e.room_id or "?"
            lines.append(
                f"- {ts} — {e.entity_id} ({room}) state={e.state}"
            )
        return "\n".join(lines)

    def _render_body(
        self,
        batch: list[MotionEvent],
        inference: MotionInference,
        *,
        security: bool,
    ) -> str:
        """Render the positional-inference body for the observation note."""
        header = "motion-security" if security else "motion-pipeline"
        current_room = inference.current_room or "?"
        confidence = inference.confidence
        person = inference.person_hypothesis or "?"
        person_conf = inference.person_confidence
        next_room = inference.next_room_hypothesis or "?"
        next_conf = inference.next_room_confidence

        def _fmt_conf(value: Optional[float]) -> str:
            return f"{value:.2f}" if isinstance(value, float) else "n/a"

        return (
            f"**{header} — batch of {len(batch)} motion event(s)**\n\n"
            f"- current room: **{current_room}** (conf {_fmt_conf(confidence)})\n"
            f"- person: **{person}** (conf {_fmt_conf(person_conf)})\n"
            f"- next room hypothesis: **{next_room}** "
            f"(conf {_fmt_conf(next_conf)})\n\n"
            f"_reasoning: {inference.reasoning}_\n\n"
            "**batch events:**\n"
            f"{self._render_batch_lines(batch)}\n"
        )


# ---------------------------------------------------------------------------
# Type hints exported for callers


# Type alias so callers (wake_loop) can annotate the dependency without
# importing the dataclass module. The factory is the public seam.
PipelineFactory = Callable[[], MotionPipeline]
NoteWriter = Callable[..., object]
ClassifyAwaitable = Callable[
    [list[MotionEvent], Iterable[MotionEvent]],
    Awaitable[MotionInference],
]
