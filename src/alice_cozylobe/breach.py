"""Breach detection — trail-shape classifier for the cozylobe motion pipeline.

Replaces the Phase 2 night-hours-only ``is_security_class`` heuristic
with a four-case classifier driven by:

* the home alarm state (only ``armed_home`` / ``armed_away`` arm the
  detector — disarmed events are always silent),
* the recent motion trail's shape (ingress-traced, bedroom-originated,
  isolated, or middle-of-house propagating),
* the room registry's adjacency graph (read from a loaded
  :class:`alice_cozylobe.cortex.Vault`).

Background. Issue: the legacy ``surface_threshold`` actionable trigger
fires when ``person == unknown AND security_class == true AND
confidence >= 0.7``. ``person == unknown`` is always true at the
bootstrap stage (Phase 5 statistical classifier hasn't accumulated
enough per-person evidence), and ``security_class`` was set purely by a
UTC hour gate — so every nighttime motion event surfaced as actionable.
Five fired overnight 2026-05-27→28; the Kitchen 3:41 EDT one was a
classic PIR misfire.

Jason's spec (Signal, 2026-05-28 08:21–08:24 EDT):

    "we're not concerned with 'Unknown' person. We're concerned with
    breach events. After we go to bed, motion that follows a path from
    home ingress. Motion that originates from bedroom/hallway is fine
    too."

    "If the motion is isolate[d], single firing sensor in middle of
    house. Just ignore it. If it starts in middle of house and starts
    moving around (not a PIR misfire), alert."

The four cases (in the order this module evaluates them):

1. **Trail traces back to ingress room** → actionable breach.
2. **Trail originates from bedroom/hallway** → silent (interior
   movement after bed-time is the family wandering around).
3. **Single isolated PIR firing in middle of house, no other sensors
   in the ±2 min window** → silent (PIR misfire).
4. **Middle-of-house motion that propagates** (two or more distinct
   sensors within 2 min, not originating from bedroom/hallway/ingress)
   → actionable.

This module is the pure decision layer. It does not poll HA, does not
write to the vault, does not call qwen. The alarm-state cache
(``AlarmStateCache``) is the only stateful piece; everything else is
function-shaped so the tests don't have to spin a daemon up.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .motion import MotionEvent


__all__ = [
    "DEFAULT_ALARM_POLL_INTERVAL_S",
    "DEFAULT_HA_ALARM_ENTITY",
    "DEFAULT_HA_TOKEN_PATH",
    "DEFAULT_HA_URL",
    "DEFAULT_ISOLATED_WINDOW_S",
    "DEFAULT_TRAIL_LOOKBACK_S",
    "DEFAULT_TRAIL_LOOKBACK_EVENTS",
    "INGRESS_ROOMS",
    "BEDROOM_ROOMS",
    "HALLWAY_ROOMS",
    "ARMED_STATES",
    "AlarmStateCache",
    "TrailClassification",
    "classify_trail",
    "is_breach_event",
]


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants

# Ingress rooms. Per the room registry (see
# ~/alice-mind/cozylobe-cortex/rooms/) these are the rooms with an
# exterior boundary:
#
# * Playroom — basement walkout door
# * Laundry Room — garage door (mud room)
# * Entranceway — front door
# * Office — balcony with outdoor stairs to ground
#
# A motion trail that reaches the home interior via any of these rooms
# is the canonical breach signature.
INGRESS_ROOMS: frozenset[str] = frozenset(
    {
        "Playroom",
        "Laundry Room",
        "Entranceway",
        "Office",
    }
)


# Bedrooms — motion originating here is interior movement (the family
# getting up, kid wandering to the bathroom, etc). Per Jason's spec,
# bedroom-origin motion is silent regardless of confidence.
BEDROOM_ROOMS: frozenset[str] = frozenset(
    {
        "Master Bedroom",
        "Nursery",
        "Guest Bedroom",
    }
)


# Hallways — same logic as bedrooms (interior transit). Any room whose
# title contains "Hallway" is treated as a hallway; the explicit set
# below catches the named rooms the registry already carries.
HALLWAY_ROOMS: frozenset[str] = frozenset(
    {
        "Hallway",
        "Kitchen Hallway",
        "Office Hallway",
        "Entrance Hallway",
    }
)


# Alarm-control-panel states that arm the breach detector. Anything not
# in this set (disarmed, pending, arming, triggered) leaves the breach
# detector off — every motion event becomes silent on the breach path.
ARMED_STATES: frozenset[str] = frozenset({"armed_home", "armed_away"})


# Trail-back window. When a motion event lands, we look at the previous
# 5 minutes (or the previous 10 trail entries, whichever is shorter)
# to decide where the trail started. Jason's spec: "After we go to bed,
# motion that follows a path from home ingress" — 5 min is generous
# enough to catch a slow walk from the front door to the kitchen.
DEFAULT_TRAIL_LOOKBACK_S: float = 5 * 60.0
DEFAULT_TRAIL_LOOKBACK_EVENTS: int = 10


# Isolated-PIR window. If exactly one sensor fires inside the ±2 minute
# window centred on the current event, classify as isolated → silent
# (PIR misfire). Two-minute matches the spec verbatim.
DEFAULT_ISOLATED_WINDOW_S: float = 2 * 60.0


# Alarm-state poll cadence. The HA SSE event stream doesn't currently
# carry alarm_control_panel updates through cozyhem-engine, so we poll
# the REST API. 60 s is fast enough that the worst-case "we just armed
# the alarm" lag is a single missed-event window, slow enough to be
# invisible against the HA API's normal load.
DEFAULT_ALARM_POLL_INTERVAL_S: float = 60.0


# HA endpoints + auth. Defaults match the deployment described in
# CLAUDE.md (10.20.30.229:8123, alarm_control_panel.home_alarm,
# bearer token in ~/.ha_token). All three are configurable on
# :class:`AlarmStateCache` so tests don't depend on the real network.
DEFAULT_HA_URL: str = "http://10.20.30.229:8123"
DEFAULT_HA_ALARM_ENTITY: str = "alarm_control_panel.home_alarm"
DEFAULT_HA_TOKEN_PATH: Path = Path.home() / ".ha_token"


# ---------------------------------------------------------------------------
# Helpers


def _is_hallway(room: Optional[str]) -> bool:
    """Treat any room whose title contains ``Hallway`` (case-sensitive
    on the slug — the vault uses Title Case) as a hallway, in addition
    to the explicit :data:`HALLWAY_ROOMS` set.

    Catches future hallway notes the registry hasn't enumerated yet
    without forcing the operator to update this module on every new
    "X Hallway" note.
    """
    if room is None:
        return False
    if room in HALLWAY_ROOMS:
        return True
    return "Hallway" in room


def _is_interior_origin(room: Optional[str]) -> bool:
    """Bedroom-or-hallway test, per spec case 2."""
    if room is None:
        return False
    return room in BEDROOM_ROOMS or _is_hallway(room)


def _is_ingress(room: Optional[str]) -> bool:
    return room is not None and room in INGRESS_ROOMS


def _adjacent(
    a: Optional[str],
    b: Optional[str],
    adjacency: Optional[dict[str, set[str]]],
) -> bool:
    """Is room ``a`` adjacent to room ``b``?

    Same-room counts as adjacent for trail-chain purposes — a person
    lingering in the kitchen for three minutes still forms a
    contiguous trail. When ``adjacency`` is ``None`` (vault not loaded),
    we fall back to "treat every transition as adjacent" so the chain
    walk still terminates on time/proximity.
    """
    if a is None or b is None:
        return False
    if a == b:
        return True
    if adjacency is None:
        return True
    return b in adjacency.get(a, set())


# ---------------------------------------------------------------------------
# Trail classification


@dataclass(frozen=True)
class TrailClassification:
    """Outcome of one trail-shape evaluation.

    ``actionable`` is the only field downstream code consults to decide
    whether to surface; ``case`` + ``reason`` are exposed for logging
    and for the actionable-surface frontmatter so an operator reading a
    surfaced event can see WHY it fired without re-running the rules.
    """

    actionable: bool
    case: str
    reason: str


def classify_trail(
    current: MotionEvent,
    trail: Iterable[MotionEvent],
    adjacency: Optional[dict[str, set[str]]] = None,
    *,
    lookback_s: float = DEFAULT_TRAIL_LOOKBACK_S,
    lookback_events: int = DEFAULT_TRAIL_LOOKBACK_EVENTS,
    isolated_window_s: float = DEFAULT_ISOLATED_WINDOW_S,
) -> TrailClassification:
    """Evaluate the four-case breach classifier for ``current`` against
    its motion trail.

    The trail is iterated as-is (oldest-first per the
    :class:`alice_cozylobe.motion.MotionTrail` contract). ``current`` is
    the event we're classifying — it may or may not be the last entry
    in ``trail``; the classifier uses the explicit argument so callers
    don't have to coordinate the snapshot timing.

    ``adjacency`` is an optional ``room → {adjacent rooms}`` map.
    Missing (``None``) → treat every room transition as adjacent (the
    classifier degrades to a pure-timing chain walk). Recommend
    passing the real graph from the loaded vault so the chain walk
    rejects spurious far-room jumps.

    Returns a :class:`TrailClassification`. Cases:

    * ``"ingress_trail"`` → actionable. An ingress room fired within
      the lookback window AND the chain of transitions from that
      ingress event to ``current`` connects via adjacency (or via
      same-room hops).
    * ``"interior_origin"`` → silent. The earliest event in the
      lookback window lands in a bedroom or hallway. This covers
      Jason wandering downstairs from the master bedroom to the
      kitchen for water at 03:00.
    * ``"isolated_pir"`` → silent. Exactly one sensor fired in the
      ±``isolated_window_s`` window (i.e. the trail has no companion
      events for ``current``). The classic PIR misfire.
    * ``"middle_of_house_propagation"`` → actionable. Two or more
      distinct sensors fired in the ±window, but no ingress event in
      the lookback window. This is "someone is moving around in the
      house and we don't know how they got in" — alert worthy.
    * ``"middle_of_house_single"`` → silent. Catch-all when the rules
      above don't apply (e.g. the trail has multiple events all in
      the same room — sustained occupancy, not propagation).
    """
    trail_list = list(trail)
    # Window-aware lookback. We sort by timestamp so an out-of-order
    # producer doesn't break the chain walk (defensive — MotionTrail
    # is supposed to be oldest-first, but a misbehaving SSE producer
    # has surprised us before).
    window_start = current.timestamp - lookback_s
    lookback = [
        e
        for e in trail_list
        if e.timestamp >= window_start and e.timestamp <= current.timestamp
    ]
    lookback.sort(key=lambda e: e.timestamp)
    # Drop the oldest entries if the count blew past the per-event cap.
    if len(lookback) > lookback_events:
        lookback = lookback[-lookback_events:]

    # Ensure ``current`` is represented in the chain. If the caller
    # already appended it to the trail before classifying (the
    # production path does this), the trail entry and ``current`` will
    # be identical; otherwise we tack it on.
    if not lookback or lookback[-1].timestamp != current.timestamp or lookback[-1].entity_id != current.entity_id:
        lookback = lookback + [current]

    distinct_rooms = {e.room_id for e in lookback if e.room_id}

    # --- Case 1: trail traces back to ingress -------------------------
    # Look for ANY ingress room in the lookback window AND verify the
    # chain from that ingress event forward to ``current`` connects via
    # adjacency (no impossible jumps). The chain walk is forgiving:
    # consecutive events in the same room count as connected, and an
    # unknown room in the middle (None room_id) is skipped over.
    for idx, event in enumerate(lookback):
        if not _is_ingress(event.room_id):
            continue
        if _chain_connects(lookback[idx:], adjacency):
            return TrailClassification(
                actionable=True,
                case="ingress_trail",
                reason=(
                    f"trail traces back to ingress room "
                    f"'{event.room_id}' within "
                    f"{lookback_s:.0f}s window"
                ),
            )

    # --- Case 2: trail originates from bedroom/hallway ----------------
    # Earliest event in the lookback decides origin. Skip events with
    # no resolved room so a stray un-onboarded sensor doesn't hide a
    # bedroom-origin signal.
    earliest_resolved = next(
        (e for e in lookback if e.room_id is not None),
        None,
    )
    if earliest_resolved is not None and _is_interior_origin(earliest_resolved.room_id):
        return TrailClassification(
            actionable=False,
            case="interior_origin",
            reason=(
                f"trail originates from interior room "
                f"'{earliest_resolved.room_id}' (bedroom or hallway)"
            ),
        )

    # --- Case 3: isolated PIR misfire ---------------------------------
    # Single sensor in the isolated window AND that sensor is in a
    # middle-of-house room (not ingress, not bedroom, not hallway).
    iso_start = current.timestamp - isolated_window_s
    iso_end = current.timestamp + isolated_window_s
    iso_window = [
        e
        for e in trail_list
        if iso_start <= e.timestamp <= iso_end
    ]
    # Always include the current event in the isolated-window analysis.
    if current not in iso_window:
        iso_window = iso_window + [current]
    iso_sensors = {e.entity_id for e in iso_window if e.entity_id}
    if len(iso_sensors) <= 1:
        return TrailClassification(
            actionable=False,
            case="isolated_pir",
            reason=(
                f"single sensor '{current.entity_id}' fired in "
                f"±{isolated_window_s:.0f}s window — PIR misfire"
            ),
        )

    # --- Case 4: middle-of-house propagation --------------------------
    # Two or more distinct sensors in the isolated window, none of
    # which is in an ingress or bedroom/hallway room. The propagation
    # of motion without an entry point is the alert condition.
    iso_distinct_sensors = iso_sensors
    if len(iso_distinct_sensors) >= 2 and len(distinct_rooms) >= 2:
        return TrailClassification(
            actionable=True,
            case="middle_of_house_propagation",
            reason=(
                f"{len(iso_distinct_sensors)} distinct sensors fired in "
                f"±{isolated_window_s:.0f}s with no ingress in lookback"
            ),
        )

    # --- Default: middle-of-house but only one room ------------------
    # Multiple events but all in the same room — sustained occupancy,
    # not propagation. Treat as silent so we don't surface someone
    # standing still in front of a single sensor.
    return TrailClassification(
        actionable=False,
        case="middle_of_house_single",
        reason=(
            "multiple events but all in one room — sustained occupancy, "
            "not propagation"
        ),
    )


def _chain_connects(
    chain: list[MotionEvent], adjacency: Optional[dict[str, set[str]]]
) -> bool:
    """Walk a sequence of motion events and verify each consecutive
    room-to-room transition is structurally adjacent (or same-room).

    Skips events with no resolved room — those are treated as
    transparent placeholders so a single unmapped sensor in the middle
    of a trail doesn't break the chain.
    """
    prev_room: Optional[str] = None
    for event in chain:
        if event.room_id is None:
            continue
        if prev_room is None:
            prev_room = event.room_id
            continue
        if not _adjacent(prev_room, event.room_id, adjacency):
            return False
        prev_room = event.room_id
    return True


# ---------------------------------------------------------------------------
# Top-level breach predicate


def is_breach_event(
    event: MotionEvent,
    trail: Iterable[MotionEvent],
    *,
    alarm_state: str,
    adjacency: Optional[dict[str, set[str]]] = None,
    lookback_s: float = DEFAULT_TRAIL_LOOKBACK_S,
    lookback_events: int = DEFAULT_TRAIL_LOOKBACK_EVENTS,
    isolated_window_s: float = DEFAULT_ISOLATED_WINDOW_S,
) -> TrailClassification:
    """Combine alarm-state gating with the trail-shape classifier.

    When the alarm isn't armed (``armed_home`` / ``armed_away``), every
    event is silent regardless of trail shape — the four-case classifier
    only runs when Jason has armed the system. This matches the spec's
    "After we go to bed, motion that follows a path from home ingress."

    Returns the same :class:`TrailClassification` shape so callers have
    a uniform handle on case + reason for logging.
    """
    if alarm_state not in ARMED_STATES:
        return TrailClassification(
            actionable=False,
            case="alarm_not_armed",
            reason=f"alarm state is '{alarm_state}', not armed",
        )
    return classify_trail(
        event,
        trail,
        adjacency=adjacency,
        lookback_s=lookback_s,
        lookback_events=lookback_events,
        isolated_window_s=isolated_window_s,
    )


# ---------------------------------------------------------------------------
# Alarm-state cache


HttpGetFn = Callable[[str, str], tuple[int, dict]]


class AlarmStateCache:
    """Polls Home Assistant for the home alarm state, caches the value.

    The cozyhem SSE stream does not (yet) carry
    ``alarm_control_panel`` updates, so the cache polls the HA REST
    endpoint every :data:`DEFAULT_ALARM_POLL_INTERVAL_S` seconds (60 s
    default). The cache is consulted synchronously by the breach
    predicate on every motion event; the polling itself runs on a
    background asyncio task spawned by the cozylobe daemon.

    Designed to fail-open: if the HA endpoint is unreachable, the last
    known state is retained. On cold start with no successful poll
    yet, the cache returns ``"unknown"`` — which is NOT in
    :data:`ARMED_STATES`, so the breach detector stays off until HA
    answers. This is the conservative default (no false positives
    while we're still trying to talk to HA).

    Tests inject ``http_get_fn`` and ``token_loader`` so we can drive
    the cache deterministically without standing up a real HA server.
    """

    def __init__(
        self,
        *,
        ha_url: str = DEFAULT_HA_URL,
        alarm_entity: str = DEFAULT_HA_ALARM_ENTITY,
        token_path: Path = DEFAULT_HA_TOKEN_PATH,
        poll_interval_s: float = DEFAULT_ALARM_POLL_INTERVAL_S,
        http_get_fn: Optional[HttpGetFn] = None,
        token_loader: Optional[Callable[[Path], Optional[str]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._alarm_entity = alarm_entity
        self._token_path = Path(token_path)
        self._poll_interval_s = poll_interval_s
        self._http_get_fn = http_get_fn or _default_http_get
        self._token_loader = token_loader or _default_token_loader
        self._clock = clock or time.time
        # Conservative default: until we successfully poll, the cache
        # reports "unknown" — the breach detector treats this as
        # not-armed, which is the safe failure mode (no false alerts).
        self._state: str = "unknown"
        self._last_poll: float = 0.0
        self._last_success: float = 0.0

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_success(self) -> float:
        return self._last_success

    def poll(self) -> str:
        """Synchronous one-shot poll. Updates the cache, returns the
        latest state value.

        Fail-open semantics: any exception during the HTTP call leaves
        the previous state intact and logs a warning. Successful
        polls bump ``last_success`` so a daemon-side monitor can spot
        a stale cache (e.g. HA has been down for 10 minutes).
        """
        self._last_poll = self._clock()
        token = self._token_loader(self._token_path)
        if not token:
            log.warning(
                "cozylobe breach: no HA token at %s; alarm state stays %r",
                self._token_path,
                self._state,
            )
            return self._state
        url = f"{self._ha_url}/api/states/{self._alarm_entity}"
        try:
            status, body = self._http_get_fn(url, token)
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning(
                "cozylobe breach: HA poll failed (url=%s): %s", url, exc
            )
            return self._state
        if status != 200:
            log.warning(
                "cozylobe breach: HA poll returned status %d (url=%s)",
                status,
                url,
            )
            return self._state
        new_state = str(body.get("state", "")).strip().lower()
        if new_state:
            self._state = new_state
            self._last_success = self._last_poll
        return self._state

    def maybe_poll(self) -> str:
        """Poll if the last successful poll was longer than
        ``poll_interval_s`` ago. Otherwise return the cached state.

        Cheap to call on every motion event — the bulk of the work
        only runs once per minute by default.
        """
        if self._clock() - self._last_poll >= self._poll_interval_s:
            return self.poll()
        return self._state


def _default_token_loader(path: Path) -> Optional[str]:
    """Read the HA bearer token from disk.

    Tolerant of missing files (returns None so the cache stays
    fail-open). Trims trailing whitespace — the file shipped with the
    container has a trailing newline.
    """
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _default_http_get(url: str, token: str) -> tuple[int, dict]:
    """Minimal HTTP GET against the HA REST API.

    Imports ``httpx`` lazily so this module doesn't pull the
    dependency unless someone actually polls. Returns ``(status_code,
    json_body)``. Any network or parse error propagates to the caller,
    which catches it and treats the poll as a no-op.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # Short timeout: HA on the LAN should answer in well under a
    # second. If it doesn't, we'd rather log a warning and keep the
    # cached value than block the motion-event handler.
    with httpx.Client(timeout=5.0) as client:
        response = client.get(url, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body
