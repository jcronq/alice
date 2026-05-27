"""Noise-vs-notes routing for cozylobe event writes (issue #411).

Implements the routing decision and burst coalescer from
:file:`cortex-memory/research/2026-05-25-cozylobe-noise-inbox-routing-design.md`.

Background
==========

The motion pipeline (and any future telemetry-class consumer in
cozylobe) drops one fleeting note into ``inner/notes/`` per coalesce
batch. With normal home activity that's ~150-200 notes/day, all of
which keep thinking's inbox perpetually non-empty, which gates Stage D
(Rule 2a inbox-drain wins over later rules — see
``research/2026-05-25-stage-d-drought-mechanism.md``). The fix is to
split low-value sensor events off into a separate subdirectory at
``inner/notes/noise/`` that thinking's drain doesn't reach.

This module owns two things:

* :func:`should_route_to_noise` — the per-event routing decision keyed
  on ``entity_id`` patterns. Returns the *intended* route, not the
  filesystem path; callers thread the decision into their write helper.
* :class:`BurstCoalescer` — a small in-process buffer that groups
  noise-class events of the same entity type. When ≥3 events arrive
  within a 60-second window the buffer flushes as one coalesced note
  instead of N separate ones. The motion pipeline already coalesces on
  its 30s window (see :class:`alice_cozylobe.motion.MotionQueue`); this
  coalescer is for non-motion noise types (light_level / ambient /
  humidity) that don't have their own dedicated pipeline.

Thinking-side compatibility
---------------------------

``vault_state._has_pending_inbox`` and ``phase._scan_design_commissions``
both use non-recursive ``iterdir`` / ``glob("*.md")`` against
``inner/notes/``. The ``noise/`` subdirectory is therefore invisible to
the inbox-drain trigger by construction — no thinking-side change
required. (Verified 2026-05-27 against ``src/alice_thinking/vault_state.py``
and ``src/alice_thinking/phase.py``.)
"""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


__all__ = [
    "BurstCoalescer",
    "CoalesceFlush",
    "DEFAULT_COALESCE_THRESHOLD",
    "DEFAULT_COALESCE_WINDOW_S",
    "NOISE_ENTITY_PATTERNS",
    "NoiseEvent",
    "classify_entity_type",
    "should_route_to_noise",
]


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing rules
#
# Patterns are fnmatch globs (case-sensitive, shell-style) so the
# matching mirrors the throttle's ``tracked_input_entity_patterns``
# convention — operators reading one file can apply the same mental
# model to the other. Order matters only for ``classify_entity_type``'s
# debug-friendly label resolution; routing itself is just any-match.
#
# These are deliberately conservative: only entity types the design
# explicitly calls out as low-value get added. Anything the router
# doesn't recognize falls through to ``notes/`` — fail-open so a new
# entity type can't silently land in noise/ and stay invisible.

NOISE_ENTITY_PATTERNS: dict[str, tuple[str, ...]] = {
    # binary_sensor.hue_*_motion / *_motion_* — Hue Indoor Motion
    # sensors fire many times per minute during presence. The motion
    # pipeline already coalesces at 30s; this rule moves THAT batch
    # note off the inbox path.
    "motion": (
        "binary_sensor.*_motion",
        "binary_sensor.*_motion_*",
    ),
    # sensor.hue_*_light_level — continuous lux telemetry. Each
    # reading is a tiny float delta; humans don't care per-event.
    "light_level": (
        "sensor.*_light_level",
        "sensor.*_lightlevel",
        "sensor.*_illuminance",
    ),
    # Ambient temperature + humidity — slow-changing, low per-reading
    # human relevance.
    "ambient": (
        "sensor.*_ambient_temp",
        "sensor.*_ambient_temperature",
        "sensor.*_temperature",
        "sensor.*_humidity",
    ),
}


def classify_entity_type(entity_id: str) -> Optional[str]:
    """Return the noise-class label (motion / light_level / ambient)
    for ``entity_id``, or ``None`` if the entity isn't a known noise
    type.

    Used by :class:`BurstCoalescer` to bucket events by type so a flood
    of motion events doesn't get coalesced with a flood of light_level
    events. Also handy for telemetry tags.
    """
    if not entity_id:
        return None
    for label, patterns in NOISE_ENTITY_PATTERNS.items():
        for pattern in patterns:
            if fnmatch.fnmatchcase(entity_id, pattern):
                return label
    return None


def should_route_to_noise(entity_id: str) -> bool:
    """Return True iff ``entity_id`` is a low-value sensor type per
    the design's routing table.

    Fail-open: empty / unknown entity_ids route to notes/. The router
    is intentionally restrictive — only entities the design explicitly
    calls out land in noise/, so a new producer that emits a novel
    entity type stays visible in the inbox until someone updates the
    rules.
    """
    return classify_entity_type(entity_id) is not None


# ---------------------------------------------------------------------------
# Burst coalescer
#
# Most noise comes from the motion pipeline which has its own 30s
# coalesce window (:class:`alice_cozylobe.motion.MotionQueue`). This
# coalescer exists for OTHER noise types (light_level / ambient /
# humidity) that would otherwise produce one note per reading. The
# rule from the design:
#
#   ≥3 events of the same entity type within a 60s window → emit ONE
#   coalesced note covering the window, reset the buffer.
#
# If fewer than 3 events arrive in the window, the buffer flushes
# stale events as individual notes (or a small coalesced note) on the
# next periodic tick — "single event followed by 70s silence emits
# its own note" per the test contract.


DEFAULT_COALESCE_WINDOW_S = 60.0
DEFAULT_COALESCE_THRESHOLD = 3


@dataclass(frozen=True)
class NoiseEvent:
    """One event accepted by the burst coalescer.

    Lightweight on purpose — the coalescer doesn't need full
    :class:`CozyHemEvent` fields, only enough to render a useful body
    in the coalesced note. Callers translate from whatever shape they
    have at the boundary.
    """

    timestamp: float
    entity_id: str
    entity_type: str  # one of NOISE_ENTITY_PATTERNS keys
    summary: str = ""  # short human-readable line (e.g. "state=on")


@dataclass
class CoalesceFlush:
    """One flush decision returned by :meth:`BurstCoalescer.add` or
    :meth:`BurstCoalescer.flush_stale`.

    ``events`` is non-empty when a write should happen. ``coalesced``
    is True when the flush hit the ≥threshold rule (write as a single
    coalesced note); False when it's a stale-flush of one or two
    buffered events that timed out (callers can still write them as
    individual notes if they want, but the default is to write a
    single small-window note carrying whatever's there).
    """

    events: list[NoiseEvent] = field(default_factory=list)
    entity_type: str = ""
    coalesced: bool = False
    window_start: float = 0.0
    window_end: float = 0.0


class BurstCoalescer:
    """Buffer noise events by entity type, flush when the threshold or
    the window expires.

    Single-consumer; the wake loop drains its queue serially on one
    asyncio task, so no locking is needed. Per-entity-type buffers are
    independent so a fast-firing motion sensor doesn't push out
    pending light_level events.

    Memory bound: buffers are pruned on every :meth:`add` and
    :meth:`flush_stale` call, so the high-water mark is at most
    ``threshold - 1`` events per entity type per window (everything
    above the threshold gets emitted and the buffer cleared). For the
    default 3-event threshold across the 3 entity types in
    :data:`NOISE_ENTITY_PATTERNS`, that's 6 events — fixed-size,
    constant memory regardless of event rate.
    """

    def __init__(
        self,
        *,
        window_s: float = DEFAULT_COALESCE_WINDOW_S,
        threshold: int = DEFAULT_COALESCE_THRESHOLD,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s must be positive, got {window_s}")
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._window_s = window_s
        self._threshold = threshold
        self._clock = clock or time.time
        # per-entity-type buffers — small lists, append + slice-prune.
        self._buffers: dict[str, list[NoiseEvent]] = {}

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def threshold(self) -> int:
        return self._threshold

    def add(self, event: NoiseEvent) -> Optional[CoalesceFlush]:
        """Add a noise event to its type's buffer.

        Returns a :class:`CoalesceFlush` when the buffer crosses the
        threshold (≥3 in the last ``window_s`` seconds). Otherwise the
        event is buffered and ``None`` is returned — caller writes
        nothing and waits for either more events or a flush_stale
        sweep.

        Events with no resolved ``entity_type`` (caller mis-classified)
        are returned as a single-event flush so the caller still has
        something to write — fail-open posture.
        """
        if not event.entity_type:
            # Shouldn't happen — caller should call
            # ``classify_entity_type`` before constructing the event.
            # Fail-open: pass it back as a one-event flush.
            return CoalesceFlush(
                events=[event],
                entity_type=event.entity_type,
                coalesced=False,
                window_start=event.timestamp,
                window_end=event.timestamp,
            )

        buf = self._buffers.setdefault(event.entity_type, [])
        # Prune events older than the window before adding so a long
        # gap doesn't drag stale events into the new coalesce.
        cutoff = self._clock() - self._window_s
        buf[:] = [e for e in buf if e.timestamp >= cutoff]
        buf.append(event)

        if len(buf) >= self._threshold:
            flush = CoalesceFlush(
                events=list(buf),
                entity_type=event.entity_type,
                coalesced=True,
                window_start=buf[0].timestamp,
                window_end=buf[-1].timestamp,
            )
            buf.clear()
            return flush
        return None

    def flush_stale(self) -> list[CoalesceFlush]:
        """Drain buffers that have been sitting longer than the window
        without hitting the threshold.

        Caller wires this to a periodic tick (the wake loop's existing
        periodic-review cadence is the natural hook). Returns one
        flush per entity type that had stale events; empty list when
        no flushes are due.

        Each returned flush has ``coalesced=False`` to signal that the
        events should be written as a small-window note, not as a
        full "3+ event burst" coalesce. Callers can render the body
        however they want; the default helpers treat a 1-event flush
        as a regular single-event note.
        """
        now = self._clock()
        out: list[CoalesceFlush] = []
        for entity_type, buf in list(self._buffers.items()):
            if not buf:
                continue
            oldest = buf[0].timestamp
            if now - oldest < self._window_s:
                continue  # still inside the window; wait for more
            flush = CoalesceFlush(
                events=list(buf),
                entity_type=entity_type,
                coalesced=False,
                window_start=buf[0].timestamp,
                window_end=buf[-1].timestamp,
            )
            buf.clear()
            out.append(flush)
        return out

    def pending_count(self, entity_type: Optional[str] = None) -> int:
        """Sum of buffered events. Useful for telemetry and tests.

        When ``entity_type`` is given, returns the count for that
        bucket only; otherwise sums across all buckets.
        """
        if entity_type is not None:
            return len(self._buffers.get(entity_type, []))
        return sum(len(b) for b in self._buffers.values())


def render_coalesced_body(flush: CoalesceFlush) -> str:
    """Render a coalesced flush as a markdown body.

    The note body lists every event with its timestamp so the data is
    consolidated, not lost. Format matches the design's example block
    in §"Burst coalescing in noise/".

    Used by callers that don't want to roll their own renderer; tests
    rely on this shape too.
    """
    if not flush.events:
        return "(empty coalesce flush)"
    entity_type = flush.entity_type or "unknown"
    count = len(flush.events)
    if flush.coalesced:
        header = (
            f"# {entity_type} burst: {count} event(s) in "
            f"{flush.window_end - flush.window_start:.1f}s"
        )
        lead = (
            f"{count} `{entity_type}` event(s) within a "
            f"{flush.window_end - flush.window_start:.1f}-second window."
        )
    else:
        header = f"# {entity_type} stale-flush: {count} event(s)"
        lead = (
            f"{count} `{entity_type}` event(s) buffered without reaching "
            "the coalesce threshold; flushed by the periodic sweep."
        )
    lines = [header, "", lead, "", "**events:**"]
    for ev in flush.events:
        ts = time.strftime(
            "%Y-%m-%d %H:%M:%S UTC", time.gmtime(ev.timestamp)
        )
        summary = ev.summary or "(no summary)"
        lines.append(f"- {ts} — `{ev.entity_id}` ({summary})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers for callers


def coalesce_slug(flush: CoalesceFlush) -> str:
    """Build a filesystem-safe slug for a coalesced note.

    Matches the slug shape ``surfaces.build_slug`` produces so the
    note filenames sort consistently with the rest of cozylobe's
    output.
    """
    from .surfaces import build_slug  # local import to avoid cycle on read

    parts: Iterable[str] = (
        "noise",
        flush.entity_type or "unknown",
        f"x{len(flush.events)}",
    )
    return build_slug(*parts)
