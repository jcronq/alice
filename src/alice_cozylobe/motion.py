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
import fnmatch
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional, TYPE_CHECKING

from . import cortex as cortex_mod
from .events import CozyHemEvent
from .qwen_client import QwenClient, QwenUnreachable
from .surfaces import build_slug, write_observation_note, write_urgent_surface


if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .adjacency import AdjacencyInferrer
    from .guesses import Guess, GuessLifecycle
    from cozylobe_cortex.classify import ClassificationResult


__all__ = [
    "DEFAULT_BATCH_WINDOW_S",
    "DEFAULT_MAX_BATCH_SIZE",
    "DEFAULT_SECURITY_NIGHT_START_HOUR",
    "DEFAULT_SECURITY_NIGHT_END_HOUR",
    "DEFAULT_TRAIL_SIZE",
    "DEFAULT_SUBGRAPH_HOPS",
    "MOTION_ENTITY_PATTERNS",
    "MOTION_EVENT_KINDS",
    "MotionEvent",
    "MotionInference",
    "MotionPipeline",
    "MotionQueue",
    "MotionTrail",
    "build_motion_prompt",
    "build_subgraph_snapshot",
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

# Phase 4 (#381): subgraph pruning radius. The cortex snapshot in the
# classify prompt is restricted to rooms within this many adjacency
# hops of any room mentioned in the current motion trail. 2 hops keeps
# qwen grounded in the locally-relevant home topology without dumping
# the whole vault into every prompt. Configurable on
# :class:`MotionPipeline` but defaulted here so :func:`build_motion_prompt`
# stays a pure function with no global state.
DEFAULT_SUBGRAPH_HOPS = 2

# Soft ceiling on the cortex snapshot size. The design's Phase 4 brief
# calls for "under 4000 tokens for the cortex section." We approximate
# tokens as len(json) / 4 and trim rooms/sensors lists if the snapshot
# blows past the ceiling. Conservative cap; nothing crashes if exceeded.
SUBGRAPH_TOKEN_CEILING = 4000

# Event kinds we treat as "motion" for the special-class flow. The SSE
# producer emits ``motion_detected`` for the canonical Hue/CozyHem
# event; we keep the set extensible for future producers without
# rewriting the wake_loop routing.
MOTION_EVENT_KINDS: frozenset[str] = frozenset({"motion_detected"})


# Issue #393: in practice CozyHem emits motion-sensor state changes as
# ``entity:update`` with the sensor's entity_id, not as a synthetic
# ``motion_detected`` kind (the cozylobe.log evidence prior to the fix:
# 427 entity:update / 0 motion_detected over one hour, all motion
# events dropped). These fnmatch globs let
# :meth:`MotionPipeline.is_motion_event` recognize motion sensors by
# entity_id when the kind is ``entity:update``. The doubled
# ``_motion_*`` pattern catches Hue's compound naming convention where
# multi-sensor devices expose the motion channel as
# ``binary_sensor.hue_master_closet_motion_sensor_motion``. Kept
# narrower than the throttle's ``tracked_input_entity_patterns`` —
# this set is "what counts as motion specifically," not "what counts
# as input."
MOTION_ENTITY_PATTERNS: frozenset[str] = frozenset(
    {
        "binary_sensor.*_motion",
        "binary_sensor.*_motion_*",
    }
)


# CozyHem's wire-level kind for the generic state-update bus.
_ENTITY_UPDATE_KIND = "entity:update"


# Phase 5 (#399): statistical classifier override threshold. When the
# Dirichlet-Multinomial classifier in :mod:`cozylobe_cortex.classify`
# returns a person_id with confidence above this floor, we override the
# qwen-derived person hypothesis on the emitted :class:`Guess` and bump
# the guess confidence to the max of the two signals. Matches the design
# doc's Step 3 threshold (``cortex-memory/research/
# 2026-05-26-classify-integration-design.md``).
STATISTICAL_CLASSIFIER_OVERRIDE_THRESHOLD = 0.65


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
    *,
    subgraph_hops: int = DEFAULT_SUBGRAPH_HOPS,
    now_hour: Optional[int] = None,
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

    NOTE: Human-only identification (no pet-or-person fallback). Per
    Jason 2026-05-26 (see feedback memory ``project_household_no_pets``)
    the household has no pets and the robot vacuum has been dormant
    for 1+ years, so every motion event is treated as human occupancy.
    The disambiguation is WHICH person, not what species. If the
    robot vacuum is ever reactivated, revisit this comment + the
    classification rules in §4.3 of the motion-cortex design.
    """
    batch_list = list(batch)
    trail_list = list(trail)
    batch_summary = [_serialize_motion(e) for e in batch_list]
    trail_summary = [_serialize_motion(e) for e in trail_list]
    cortex_snapshot = build_subgraph_snapshot(
        vault,
        batch_list,
        trail_list,
        hops=subgraph_hops,
        now_hour=now_hour,
    )

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


def build_subgraph_snapshot(
    vault: Optional[cortex_mod.Vault],
    batch: Iterable[MotionEvent],
    trail: Iterable[MotionEvent],
    *,
    hops: int = DEFAULT_SUBGRAPH_HOPS,
    now_hour: Optional[int] = None,
) -> dict:
    """Return a JSON-safe cortex snapshot pruned to the 2-hop neighborhood
    of every room in the recent motion trail (Phase 4 of #381).

    Replaces the Phase 2 full-vault dump. Sharpening the cortex section
    to the locally-relevant subgraph keeps the qwen prompt under the
    design's 4000-token budget AND removes irrelevant rooms that were
    noise from the classifier's point of view.

    Pruning rules:

    * **Seed rooms** — every room mentioned in ``batch`` or ``trail``
      (deduped). If the trail is empty AND no vault, returns an empty
      snapshot.
    * **N-hop expansion** — for ``hops=2`` (default) the seed set is
      unioned with rooms adjacent to seeds, then unioned with rooms
      adjacent to THAT set. Operates over the vault's
      ``Room.adjacent`` frontmatter list.
    * **Sensors** — only sensors whose ``room`` lands in the pruned
      room set survive into the snapshot. Sensors with no room
      (un-onboarded) are dropped.
    * **People priors** — when ``now_hour`` is provided, every
      :class:`~alice_cozylobe.cortex.Person` whose ``time_patterns``
      reference a destination matching the hour is included with a
      short summary. ``now_hour=None`` skips the people priors
      altogether (Phase 2 callers).

    Output shape (matches the design's CORTEX_STATE section):

    .. code-block:: python

        {
            "rooms": ["Hallway", "Kitchen", "Living Room", ...],
            "sensors": [{"entity_id": "hue_kitchen_motion", "room": "Kitchen"}],
            "adjacency": {"Kitchen": ["Hallway", "Dining Room"], ...},
            "people_priors": [
                {"person": "Jason", "destination": "Kitchen-at-07:00"}
            ],
        }

    Returns ``{"rooms": [], "sensors": [], "adjacency": {}, "people_priors": []}``
    when the vault is missing OR the trail provides no usable seed
    rooms — graceful degrade matches the Phase 2 fail-open posture.
    """
    if vault is None:
        return {
            "rooms": [],
            "sensors": [],
            "adjacency": {},
            "people_priors": [],
        }

    seed_rooms = _collect_seed_rooms(batch, trail)
    if not seed_rooms:
        # Empty trail (e.g. cold start). Surface a minimal grounding
        # snapshot — every room + every adjacency, but no sensors or
        # priors. Keeps the classifier informed of the home layout on
        # the very first event without dragging in the whole vault.
        return _minimal_snapshot(vault)

    selected_titles = _expand_subgraph(vault, seed_rooms, hops=max(1, hops))

    rooms_sorted = sorted(selected_titles)

    # Sensor pruning: only sensors whose room landed in the selected
    # set survive. Drops empty/uncovered sensors so the prompt stays
    # focused on the local neighborhood.
    sensors: list[dict] = []
    for sensor in vault.sensors.values():
        if sensor.room is None:
            continue
        room_title = sensor.room.split("/", 1)[-1] if "/" in sensor.room else sensor.room
        if room_title in selected_titles:
            sensors.append({"entity_id": sensor.title, "room": room_title})
    sensors.sort(key=lambda s: s["entity_id"])

    adjacency: dict[str, list[str]] = {}
    for room in vault.rooms.values():
        if room.title not in selected_titles:
            continue
        targets = sorted(
            {
                adj.split("/", 1)[-1] if "/" in adj else adj
                for adj in room.adjacent
            }
        )
        if targets:
            adjacency[room.title] = targets

    people_priors = _select_people_priors(vault, now_hour) if now_hour is not None else []

    snapshot = {
        "rooms": rooms_sorted,
        "sensors": sensors,
        "adjacency": adjacency,
        "people_priors": people_priors,
    }
    return _trim_to_budget(snapshot)


def _collect_seed_rooms(
    batch: Iterable[MotionEvent], trail: Iterable[MotionEvent]
) -> set[str]:
    """Pull the unique set of room ids mentioned in ``batch`` + ``trail``.

    Drops events with no resolved room (sensor not in vault). Order
    doesn't matter for the seed set, so a plain set is enough.
    """
    seeds: set[str] = set()
    for event in batch:
        if event.room_id:
            seeds.add(event.room_id)
    for event in trail:
        if event.room_id:
            seeds.add(event.room_id)
    return seeds


def _expand_subgraph(
    vault: cortex_mod.Vault, seeds: set[str], *, hops: int
) -> set[str]:
    """BFS expansion from ``seeds`` through the room adjacency graph.

    Stops after ``hops`` rounds. Returns the union of all rooms
    encountered (including the seeds). Rooms not present in the vault
    are kept in the seed set so the snapshot still reports them — qwen
    can decide whether they're real or a sensor misconfiguration.
    """
    selected: set[str] = set(seeds)
    frontier: set[str] = set(seeds)
    for _ in range(hops):
        next_frontier: set[str] = set()
        for title in frontier:
            room = vault.rooms.get(f"rooms/{title}")
            if room is None:
                continue
            for adj in room.adjacent:
                adj_title = adj.split("/", 1)[-1] if "/" in adj else adj
                if adj_title not in selected:
                    next_frontier.add(adj_title)
        if not next_frontier:
            break
        selected.update(next_frontier)
        frontier = next_frontier
    return selected


def _minimal_snapshot(vault: cortex_mod.Vault) -> dict:
    """Whole-vault snapshot — used as a cold-start fallback when the
    trail is empty so qwen still gets layout grounding on the very
    first event.

    Includes every room + sensor + adjacency edge the vault carries.
    The token-budget trimmer in :func:`_trim_to_budget` will drop
    sensors first if the snapshot blows past the ceiling for a big
    vault, so the cold-start path stays bounded.
    """
    rooms = sorted(r.title for r in vault.rooms.values())
    sensors: list[dict] = []
    for sensor in vault.sensors.values():
        if sensor.room is None:
            continue
        room_title = (
            sensor.room.split("/", 1)[-1] if "/" in sensor.room else sensor.room
        )
        sensors.append({"entity_id": sensor.title, "room": room_title})
    sensors.sort(key=lambda s: s["entity_id"])
    adjacency: dict[str, list[str]] = {}
    for room in vault.rooms.values():
        targets = sorted(
            {
                adj.split("/", 1)[-1] if "/" in adj else adj
                for adj in room.adjacent
            }
        )
        if targets:
            adjacency[room.title] = targets
    return _trim_to_budget(
        {
            "rooms": rooms,
            "sensors": sensors,
            "adjacency": adjacency,
            "people_priors": [],
        }
    )


def _select_people_priors(vault: cortex_mod.Vault, hour: int) -> list[dict]:
    """Return a short list of (person, destination) pairs whose
    time-of-day window covers ``hour``.

    The destination note's ``time_window`` frontmatter is the source of
    truth (shape ``HH:MM–HH:MM``). When that's missing or unparseable,
    we fall back to the destination's filename slug (``Kitchen-at-07:00``).
    """
    out: list[dict] = []
    for person in vault.people.values():
        for dest_target in person.time_patterns:
            dest_title = dest_target.split("/", 1)[-1] if "/" in dest_target else dest_target
            dest = vault.destinations.get(f"destinations/{dest_title}")
            if dest is None:
                continue
            if _destination_covers_hour(dest, dest_title, hour):
                out.append({"person": person.title, "destination": dest_title})
    out.sort(key=lambda d: (d["person"], d["destination"]))
    return out


def _destination_covers_hour(
    dest: cortex_mod.Destination, dest_title: str, hour: int
) -> bool:
    """Best-effort: does ``dest`` cover ``hour``?

    Reads ``dest.time_window`` if present (shape ``HH:MM–HH:MM`` or
    ``HH:MM-HH:MM``). Falls back to scanning ``-at-HH:MM`` in the
    destination title. Window crossing midnight is supported.
    """
    if dest.time_window:
        window = dest.time_window.replace("–", "-")  # accept en-dash + hyphen
        if "-" in window:
            start_s, end_s = window.split("-", 1)
            try:
                start_h = int(start_s.split(":")[0])
                end_h = int(end_s.split(":")[0])
            except ValueError:
                start_h = end_h = -1
            if 0 <= start_h <= 23 and 0 <= end_h <= 23:
                if start_h <= end_h:
                    return start_h <= hour <= end_h
                return hour >= start_h or hour <= end_h
    # Fallback: parse "Kitchen-at-07:00" → 7.
    if "-at-" in dest_title:
        try:
            tail = dest_title.split("-at-", 1)[1]
            anchor_h = int(tail.split(":")[0])
        except (IndexError, ValueError):
            return False
        # Treat the anchor as a 2-hour window around the hour for the
        # fallback case (07:00 → covers 06-08).
        return abs(anchor_h - hour) <= 1
    return False


def _trim_to_budget(snapshot: dict) -> dict:
    """Trim sensors then adjacency entries when the snapshot exceeds the
    soft token ceiling. Rooms list stays — that's the load-bearing
    grounding information for the classifier.
    """
    estimated = sum(
        len(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
        for v in snapshot.values()
    )
    if estimated <= SUBGRAPH_TOKEN_CEILING * 4:
        return snapshot
    # Drop sensors first — entity_ids tend to dominate the byte count
    # and the adjacency graph carries more reasoning value.
    snapshot = dict(snapshot)
    if snapshot.get("sensors"):
        snapshot["sensors"] = []
        estimated = sum(
            len(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
            for v in snapshot.values()
        )
    if estimated <= SUBGRAPH_TOKEN_CEILING * 4:
        return snapshot
    # If still too big, drop people priors next.
    snapshot["people_priors"] = []
    return snapshot


async def classify_motion_batch(
    batch: list[MotionEvent],
    trail: Iterable[MotionEvent],
    *,
    qwen_client: QwenClient,
    vault: Optional[cortex_mod.Vault] = None,
    subgraph_hops: int = DEFAULT_SUBGRAPH_HOPS,
    now_hour: Optional[int] = None,
) -> MotionInference:
    """Classify one motion batch via local qwen.

    Returns a :class:`MotionInference` with the positional-inference
    fields parsed out of qwen's JSON response. Raises
    :class:`QwenUnreachable` on network or parse failure — the caller
    (the pipeline) catches and degrades gracefully.
    """
    prompt = build_motion_prompt(
        batch, trail, vault, subgraph_hops=subgraph_hops, now_hour=now_hour
    )
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
        vault_root: Optional[Path] = None,
        lifecycle: Optional["GuessLifecycle"] = None,
        write_surface: Optional[Callable[..., object]] = None,
        adjacency_inferrer: Optional["AdjacencyInferrer"] = None,
        record_trajectory_fn: Optional[
            Callable[[Path, Optional[str], str, str, datetime], object]
        ] = None,
        subgraph_hops: int = DEFAULT_SUBGRAPH_HOPS,
    ) -> None:
        self._qwen = qwen_client
        self._vault = vault
        self._trail = trail or MotionTrail()
        self._queue = queue or MotionQueue(clock=clock)
        self._write_note = write_note or write_observation_note
        self._clock = clock or time.time
        self._localtime = localtime or time.localtime
        self._security_predicate = security_predicate
        # Phase 3 (#380): guess emission + lifecycle wiring. The pipeline
        # writes a guess note to ``vault_root/guesses/`` after every
        # successful classify and runs the lifecycle's
        # ``process_new_event`` on every motion event so self-evident
        # confirmation/refutation fires within the 60-second window.
        # ``vault_root`` defaults to None — guess writes are skipped and
        # only the legacy observation-note path runs, which is what
        # Phase 2's tests rely on.
        self._vault_root = Path(vault_root) if vault_root is not None else None
        self._lifecycle = lifecycle
        self._write_surface = write_surface or write_urgent_surface
        # Phase 4 (#381): adjacency inferrer + trajectory recorder.
        # Both are optional so existing Phase 2/3 callers and tests can
        # keep their constructor calls unchanged. When ``adjacency_inferrer``
        # is None, observe() is skipped; when ``record_trajectory_fn`` is
        # None, we lazy-import the default implementation from
        # :mod:`alice_cozylobe.trajectories`. The latter is wrapped so
        # tests can inject a recorder spy without importing the module.
        self._adjacency_inferrer = adjacency_inferrer
        self._record_trajectory_fn = record_trajectory_fn
        self._subgraph_hops = subgraph_hops
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
        """Return True iff ``event`` is a motion-sensor firing.

        Two arms:

        * ``event.kind`` is in :data:`MOTION_EVENT_KINDS` (the original
          ``motion_detected`` kind, kept for future producers).
        * ``event.kind == "entity:update"`` AND ``event.entity_id``
          matches one of :data:`MOTION_ENTITY_PATTERNS`. Issue #393:
          CozyHem doesn't emit ``motion_detected`` — it emits
          ``entity:update`` with a motion-sensor entity_id, so the
          motion pipeline must recognize the entity_id-shape too.

        Centralized here so wake_loop doesn't duplicate the rules.
        """
        if event.kind in MOTION_EVENT_KINDS:
            return True
        if event.kind == _ENTITY_UPDATE_KIND and event.entity_id:
            for pattern in MOTION_ENTITY_PATTERNS:
                if fnmatch.fnmatchcase(event.entity_id, pattern):
                    return True
        return False

    async def handle(self, event: CozyHemEvent) -> None:
        """Process one motion event through the full pipeline.

        Steps:

        1. Build a :class:`MotionEvent` (sensor → room lookup via the
           cortex vault).
        2. Append to the trail (always, even on security-class).
        3. Run the lifecycle's :meth:`process_new_event` so any
           recent pending guess gets self-evident confirmation/refutation
           applied against this fresh observation BEFORE the classify
           runs and overwrites the prediction.
        4. If security-class, classify + write the note IMMEDIATELY,
           bypassing the queue.
        5. Otherwise, add to the queue and flush if the window has
           elapsed or the batch is full.

        Exceptions in the classify or note write are logged but do
        NOT propagate — the wake loop's per-event error handling sees
        them as warnings, not as a reason to kill the loop. Same
        pattern as the existing _classify graceful-degrade.
        """
        motion = MotionEvent.from_cozyhem(event, vault=self._vault)
        self._trail.append(motion)

        # Phase 4 (#381): adjacency inferrer.observe runs every event
        # against the latest trail. Walks consecutive pairs and counts
        # any non-adjacent pair within the 30s window — promotes
        # crossings of the 5/10/20/50 thresholds to inline edges on the
        # room notes. Fail-open: an adjacency write failure must never
        # take the motion pipeline down, so observe() never raises.
        if self._adjacency_inferrer is not None:
            try:
                self._adjacency_inferrer.observe(self._trail.snapshot())
            except Exception as exc:  # noqa: BLE001 - fail-open
                log.warning(
                    "cozylobe motion: adjacency.observe raised: %s", exc
                )

        # Phase 3: lifecycle gets first crack at the event — confirm /
        # refute pending guesses whose 60s window is still open. We
        # swallow exceptions because a broken lifecycle should never
        # take the motion pipeline down (same fail-open posture as the
        # classify path).
        if self._lifecycle is not None:
            try:
                self._lifecycle.process_new_event(motion)
            except Exception as exc:  # noqa: BLE001 - fail-open
                log.warning(
                    "cozylobe motion: lifecycle.process_new_event raised: %s",
                    exc,
                )

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

    def _current_hour(self) -> Optional[int]:
        """Return the local hour-of-day (0..23) for the people-prior
        selection in the cortex snapshot. Falls back to None when the
        localtime hook isn't injected — keeps Phase 2 tests stable.
        """
        try:
            return self._localtime(self._clock()).tm_hour
        except Exception:  # noqa: BLE001 - defensive
            return None

    def _record_trajectory_safely(
        self, guess: Optional["Guess"]
    ) -> None:
        """Phase 4: persist a typed-edge trajectory observation if the
        classify produced a next-room hypothesis AND we know which
        room the person came from.

        ``from_room`` is the second-to-last room in the trail (the room
        before the guess's current room); ``to_room`` is the predicted
        next-room hypothesis. Person defaults to "unknown" when the
        classifier didn't propose one. Fail-open on any error —
        trajectory IO must never block the motion pipeline.
        """
        if self._vault_root is None:
            return
        if guess is None or not guess.next_room_hypothesis:
            return
        if not guess.room:
            return
        from_room = self._previous_trail_room(guess.room)
        if not from_room:
            # No prior room in the trail — we can't anchor a
            # from→to edge yet. Trajectory needs both endpoints.
            return
        try:
            recorder = self._record_trajectory_fn
            if recorder is None:
                from .trajectories import record_trajectory

                recorder = record_trajectory
            recorder(
                self._vault_root,
                guess.person,
                from_room,
                guess.next_room_hypothesis,
                guess.updated or guess.created or datetime.now(timezone.utc),
            )
        except OSError as exc:
            log.warning(
                "cozylobe motion: trajectory write failed: %s", exc
            )
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning(
                "cozylobe motion: trajectory record raised: %s", exc
            )

    def _previous_trail_room(self, current_room: str) -> Optional[str]:
        """Walk the trail backwards from the latest event, return the
        first room that isn't ``current_room``. Used as the ``from_room``
        anchor for trajectory recording.
        """
        for event in reversed(self._trail.snapshot()):
            if event.room_id and event.room_id != current_room:
                return event.room_id
        return None

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
                subgraph_hops=self._subgraph_hops,
                now_hour=self._current_hour(),
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
        # Phase 5 (#399): run the statistical person-identification
        # classifier alongside qwen. Fail-open — a classifier exception
        # must not take the pipeline down. The result enriches the
        # emitted Guess in :meth:`_emit_guess`.
        classification = self._run_statistical_classifier()
        self._write_inference_note(
            batch, inference, security=security, classification=classification
        )

    def _write_inference_note(
        self,
        batch: list[MotionEvent],
        inference: MotionInference,
        *,
        security: bool,
        classification: Optional["ClassificationResult"] = None,
    ) -> None:
        """Render the inference as a fleeting note in inner/notes/.

        Tagged ``motion-pipeline`` so thinking can spot the new Phase 2
        flow during drain (plus ``lobe-observation`` for compatibility
        with the existing drain rules, and ``motion-security`` when the
        security fast-path fired). Phase 3 also writes a guess record
        to ``cozylobe-cortex/guesses/`` and routes the surface tier
        per design §4.5.

        Phase 5 (#399): ``classification`` carries the statistical
        person-id result (or None on failure). It's threaded into
        :meth:`_emit_guess` so the persisted Guess can carry the
        enriched person + confidence.
        """
        slug_parts = ["motion", "batch", str(len(batch))]
        if inference.current_room:
            slug_parts.append(inference.current_room)
        slug = build_slug(*slug_parts)

        # Phase 3: emit the durable guess record before the observation
        # note, so the note's body can reference the guess id once we
        # ship the cross-link in a follow-up. Today the guess and the
        # note are written independently.
        guess = self._emit_guess(batch, inference, classification=classification)
        # Phase 4 (#381): persist a trajectory edge if the classify
        # proposed a next-room hypothesis. The guess-lifecycle's
        # self-evident confirmation will validate/refute on the next
        # event — this write is the durable record so confirmed
        # trajectories accumulate weight over time.
        self._record_trajectory_safely(guess)
        tier = self._classify_surface_tier(guess, security=security)

        tags = ["lobe-observation", "motion-pipeline", f"surface-tier:{tier}"]
        if security:
            tags.append("motion-security")
        if guess is not None and guess.guess_id is not None:
            tags.append(f"guess-id:{guess.guess_id}")

        body = self._render_body(batch, inference, security=security)
        try:
            self._write_note(body, slug=slug, tags=tuple(tags))
        except OSError as exc:
            log.warning("cozylobe motion: note write failed: %s", exc)

        if tier == "actionable" and guess is not None:
            self._emit_actionable_surface(guess, batch, security=security)

    def _emit_guess(
        self,
        batch: list[MotionEvent],
        inference: MotionInference,
        *,
        classification: Optional["ClassificationResult"] = None,
    ) -> Optional["Guess"]:
        """Construct a guess record and persist it to the vault.

        Returns the in-memory guess so :meth:`_write_inference_note`
        can carry the id onto the observation note's tags. Returns
        None when no vault_root is configured (Phase 2 callers that
        haven't migrated yet) or when the write fails — fail-open.

        Phase 5 (#399): when ``classification`` carries a confident
        person id from the statistical Dirichlet-Multinomial classifier
        (confidence above
        :data:`STATISTICAL_CLASSIFIER_OVERRIDE_THRESHOLD`), we override
        the qwen-derived person hypothesis on the guess and raise the
        guess confidence to the max of the two signals. Qwen's
        contextual reasoning still drives ``room`` + ``next_room`` —
        the statistical model only contributes person identity.
        """
        if self._vault_root is None:
            return None
        # Lazy import to keep the motion.py↔guesses.py edge from
        # forming an import cycle at module load.
        from .guesses import guess_from_inference, write_guess

        try:
            now = datetime.now(timezone.utc)
            guess = guess_from_inference(
                inference,
                batch,
                trail_window=len(self._trail),
                now=now,
            )
            # Phase 5: stats-classifier override. Only fires when the
            # classifier produced a confident person_id; otherwise the
            # qwen-derived person field carries through unchanged.
            if (
                classification is not None
                and classification.person_id
                and classification.confidence
                > STATISTICAL_CLASSIFIER_OVERRIDE_THRESHOLD
            ):
                guess.person = classification.person_id
                guess.confidence = max(guess.confidence, classification.confidence)
            write_guess(self._vault_root, guess)
            return guess
        except OSError as exc:
            log.warning("cozylobe motion: guess write failed: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning("cozylobe motion: guess emit raised: %s", exc)
            return None

    def _run_statistical_classifier(self) -> Optional["ClassificationResult"]:
        """Run :func:`cozylobe_cortex.classify.classify_from_trail` on
        the current motion trail.

        Phase 5 (#399). Translates this module's :class:`MotionEvent`
        records into the classifier's own dataclass shape (different
        field names: ``timestamp`` vs ``ts``, ``room_id`` vs ``room``)
        and swallows every exception path so a classifier import error,
        empty profile dir, or arithmetic edge case never propagates to
        the motion pipeline. Returns ``None`` on failure or empty trail.
        """
        try:
            from cozylobe_cortex.classify import (
                MotionEvent as ClassifyMotionEvent,
                classify_from_trail,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning("cozylobe motion: classify.py import failed: %s", exc)
            return None

        try:
            trail_snapshot = self._trail.snapshot()
            classify_events: list[ClassifyMotionEvent] = []
            for ev in trail_snapshot:
                if not ev.room_id:
                    # Skip events with no resolved room — the
                    # classifier's room-preference scoring needs a room
                    # label and a None would be silently treated as the
                    # string "None".
                    continue
                classify_events.append(
                    ClassifyMotionEvent(
                        entity_id=ev.entity_id,
                        room=ev.room_id,
                        ts=datetime.fromtimestamp(ev.timestamp, tz=timezone.utc),
                        state=ev.state,
                    )
                )
            if not classify_events:
                return None
            return classify_from_trail(classify_events)
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning("cozylobe motion: classify_from_trail failed: %s", exc)
            return None

    def _classify_surface_tier(
        self,
        guess: Optional["Guess"],
        *,
        security: bool,
    ) -> str:
        """Pick the surface tier for this inference.

        Uses :func:`surface_threshold` with ``unexpected=security`` —
        Phase 2's security heuristic (motion in the nighttime window)
        is the closest stand-in for the design's "unexpected event"
        flag until Phase 4 ships the novel-pattern detector. Falls
        back to the legacy "log" tier when no guess was emitted so
        existing Phase 2 tests (no vault_root) keep their tag shape.
        """
        if guess is None:
            return "log"
        from .guesses import surface_threshold

        return surface_threshold(guess, unexpected=security)

    def _emit_actionable_surface(
        self,
        guess: "Guess",
        batch: list[MotionEvent],
        *,
        security: bool,
    ) -> None:
        """Drop a surface file into ``inner/surface/`` for the speaking
        daemon to pick up. Reserved for actionable-tier guesses.

        Frontmatter mirrors the shape :func:`write_urgent_surface`
        emits for other lobes; the speaking watcher already routes on
        ``surface_type`` so cozylobe doesn't need a custom code path.
        """
        slug = build_slug(
            "guess",
            guess.person or "unknown",
            guess.room or "unknown",
        )
        body_lines = [
            f"**Actionable motion inference** — {guess.title}",
            "",
            f"- person: {guess.person or 'unknown'}",
            f"- room: {guess.room or 'unknown'}",
            f"- confidence: {guess.confidence:.2f}",
            f"- security-class: {'yes' if security else 'no'}",
        ]
        if guess.next_room_hypothesis:
            body_lines.append(
                f"- next-room hypothesis: {guess.next_room_hypothesis}"
            )
        if guess.body:
            body_lines.append("")
            body_lines.append(guess.body.strip())
        body = "\n".join(body_lines)
        extra = {
            "guess_id": guess.guess_id or "",
            "confidence": f"{guess.confidence:.3f}",
            "person": guess.person or "unknown",
            "room": guess.room or "unknown",
            "security": "true" if security else "false",
        }
        try:
            self._write_surface(
                body,
                slug=slug,
                surface_type="cozylobe-actionable",
                extra_frontmatter=extra,
            )
        except OSError as exc:
            log.warning("cozylobe motion: actionable surface write failed: %s", exc)
        except TypeError:
            # Fallback for test injects that don't accept the full kwargs.
            try:
                self._write_surface(body, slug=slug)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "cozylobe motion: actionable surface fallback failed: %s", exc
                )

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
