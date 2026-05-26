"""Diff-aware throttle for cozylobe SSE events (issue #371).

The walking-skeleton path was "one event = one qwen classify = one
agent dispatch = one note." On a quiet day cozylobe still classifies
hundreds of ``entity:update`` events because circadian dimming pushes
a tiny brightness / color-temp delta on every tick. Most of those
events are not interesting on their own — only the aggregate
"circadian ramp is happening" signal matters.

This module inserts a diff-aware throttle between
:class:`alice_cozylobe.sse_consumer.SSEConsumer` and the
:meth:`alice_cozylobe.wake_loop.WakeLoop._classify` call. Each event
gets one of three outcomes from :meth:`Throttle.handle`:

* ``pass`` — event is interesting; proceed with the normal classify
  + agent + note path.
* ``drop`` — event is a routine micro-delta inside the coalesce
  window for that entity; skip the classify entirely and don't write
  a note. Telemetry still fires so the suppression is visible.
* ``summary`` — event is a routine micro-delta but the coalesce window
  has elapsed for this entity, so we emit ONE synthesized
  ``entity:update_summary`` event carrying the entity's current state
  + the count of suppressed events since the last emit. The wake loop
  treats it like a normal event (one classify, one note) so thinking
  still sees the activity, just at the cadence configured in the yaml.

Configuration lives at ``~/alice-mind/config/cozylobe-throttle.yaml``.
Agents (thinking, speaking, other lobes) can edit it at runtime; the
throttle re-reads via an mtime check on the next event, so changes
take effect within one wake cycle without a daemon restart. A starter
file ships in this repo at ``config/cozylobe-throttle.yaml``; the
daemon copies it into the user-config path on first start when the
target doesn't already exist.

Default rule set (matches issue #371's acceptance criteria):

* ``entity:update`` events with a tracked numeric field
  (``brightness``, ``color_temp``) whose only changing field is
  within ``threshold`` of the last emitted value → suppress and
  coalesce into one summary every ``coalesce_window_s`` seconds per
  entity.
* ``state`` field transitions (``"on"`` ↔ ``"off"``, or any string
  change) → always pass.
* Non-``entity:update`` kinds (motion, doorbell, scene, novel) →
  always pass. Fail-open on anything the throttle doesn't recognize.
* Per-entity overrides via ``always_pass: [entity_id, ...]`` so
  thinking can pin a specific sensor as "always interesting" without
  touching the kind-level rules.
"""

from __future__ import annotations

import logging
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .events import CozyHemEvent


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_INPUT_KINDS",
    "DEFAULT_SHIPPED_CONFIG_PATH",
    "ThrottleConfig",
    "ThrottleDecision",
    "Throttle",
]


log = logging.getLogger(__name__)


# The user-editable config lives in alice-mind so agents have the same
# read/write surface they use for the rest of their durable
# configuration. The daemon seeds it from the shipped default on first
# start if it doesn't exist.
DEFAULT_CONFIG_PATH = pathlib.Path.home() / "alice-mind" / "config" / "cozylobe-throttle.yaml"

# The repo-shipped starter file. Lives at ``config/cozylobe-throttle.yaml``
# at the alice-runtime checkout root. Resolved relative to this module
# so the daemon doesn't depend on the cwd at startup.
DEFAULT_SHIPPED_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "config"
    / "cozylobe-throttle.yaml"
)


# Event kind we treat as the "tunable" path. Other kinds always pass.
_ENTITY_UPDATE_KIND = "entity:update"


# INPUT_KINDS allowlist — the primary filter applied at the SSE entry
# point in :class:`alice_cozylobe.wake_loop.WakeLoop` (Phase 2 of #379).
# Any event whose kind is NOT on this list is dropped silently before
# the throttle, before any classify, before any log. OUTPUT events
# (circadian brightness updates, propagated light states after a scene
# change, automation setpoint writes) never enter the pipeline.
#
# The list is configurable in the same yaml as the throttle so agents
# can extend it at runtime without a daemon restart (mtime reload).
# Defaults match the motion-cortex design's "INPUT_KINDS allowlist"
# section (cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md
# §1.1). See also the issue #379 acceptance criteria.
DEFAULT_INPUT_KINDS: frozenset[str] = frozenset(
    {
        # Motion sensors (the special-class flow lives in motion.py).
        "motion_detected",
        # Discrete user inputs.
        "doorbell_pressed",
        "button_pressed",
        # Door / window contact sensors.
        "door_opened",
        "door_closed",
        "window_opened",
        "window_closed",
        # Environment sensors (only readings — setpoint writes are OUTPUT).
        "temperature_changed",
        # Security sensors.
        "smoke_detected",
        "glass_break",
        "water_leak",
        # Camera events (person identified, motion in frame).
        "camera_event",
        # User-initiated mode/scene changes (auto-changes are OUTPUT).
        "scene_changed",
        "mode_changed",
        # Lock state changes (user-initiated).
        "lock_state_changed",
    }
)


@dataclass
class ThrottleConfig:
    """Runtime-tunable filter config.

    Attributes:
        tracked_numeric_fields: Map ``field_name`` → ``threshold``. An
            ``entity:update`` whose only non-state delta is in one of
            these fields, and whose new value is within ``threshold`` of
            the last emitted value, is a candidate for suppression.
            Default: ``{"brightness": 0.05, "color_temp": 0.05}``.
        coalesce_window_s: After suppressing one or more events on a
            given entity, emit one summary every ``coalesce_window_s``
            seconds. Default 300s (5 min) — matches issue #371's
            "coalesce into one summary per entity every 5 min" rule.
        always_pass_entities: Per-entity escape hatch. Any entity_id in
            this set is never suppressed, regardless of field deltas.
            Lets thinking pin a single sensor without rewriting the
            kind-level rules.
        always_pass_kinds: Per-kind escape hatch. Any event whose kind
            is in this set is never suppressed. Default includes the
            CRITICAL kinds and ``motion_detected`` / ``scene_changed``
            — anything that's not a routine state delta.
    """

    tracked_numeric_fields: dict[str, float] = field(
        default_factory=lambda: {"brightness": 0.05, "color_temp": 0.05}
    )
    coalesce_window_s: float = 300.0
    always_pass_entities: frozenset[str] = field(default_factory=frozenset)
    always_pass_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "doorbell_pressed",
                "smoke_detected",
                "glass_break",
                "motion_detected",
                "scene_changed",
                "mode_changed",
            }
        )
    )
    # Phase 2 (#379): INPUT_KINDS allowlist applied BEFORE the throttle.
    # Empty set means "accept all" — fail-open for legacy callers that
    # don't set this field. The shipped yaml carries the canonical
    # default list (see :data:`DEFAULT_INPUT_KINDS`).
    input_kinds: frozenset[str] = field(
        default_factory=lambda: DEFAULT_INPUT_KINDS
    )

    @classmethod
    def default(cls) -> "ThrottleConfig":
        """Return the in-process default (matches the shipped yaml)."""
        return cls()

    @classmethod
    def from_mapping(cls, raw: Any) -> "ThrottleConfig":
        """Build a config from a parsed-yaml mapping.

        Unknown keys are ignored on purpose — agents may extend the
        yaml with hints we don't yet read; the throttle should never
        crash on a forward-compatible config.
        """
        if not isinstance(raw, dict):
            log.warning(
                "cozylobe throttle: config root is not a mapping "
                "(%s); falling back to defaults",
                type(raw).__name__,
            )
            return cls.default()

        cfg = cls.default()

        tnf = raw.get("tracked_numeric_fields")
        if isinstance(tnf, dict):
            parsed: dict[str, float] = {}
            for key, val in tnf.items():
                if not isinstance(key, str):
                    continue
                try:
                    parsed[key] = float(val)
                except (TypeError, ValueError):
                    log.warning(
                        "cozylobe throttle: tracked_numeric_fields[%r] "
                        "is not a number (%r); skipping",
                        key,
                        val,
                    )
            cfg.tracked_numeric_fields = parsed or cfg.tracked_numeric_fields

        cw = raw.get("coalesce_window_s")
        if cw is not None:
            try:
                cfg.coalesce_window_s = float(cw)
            except (TypeError, ValueError):
                log.warning(
                    "cozylobe throttle: coalesce_window_s is not a "
                    "number (%r); keeping default %.1f",
                    cw,
                    cfg.coalesce_window_s,
                )

        ape = raw.get("always_pass_entities")
        if isinstance(ape, (list, tuple, set, frozenset)):
            cfg.always_pass_entities = frozenset(
                str(x) for x in ape if isinstance(x, str)
            )

        apk = raw.get("always_pass_kinds")
        if isinstance(apk, (list, tuple, set, frozenset)):
            cfg.always_pass_kinds = frozenset(
                str(x) for x in apk if isinstance(x, str)
            )

        # Phase 2 (#379): input_kinds allowlist. Missing key → keep the
        # default set baked into the dataclass. Explicit empty list →
        # empty set, which the WakeLoop interprets as "accept all"
        # (fail-open) so a botched edit can't take cozylobe offline.
        ik = raw.get("input_kinds")
        if isinstance(ik, (list, tuple, set, frozenset)):
            cfg.input_kinds = frozenset(
                str(x) for x in ik if isinstance(x, str)
            )

        return cfg

    @classmethod
    def load(cls, path: pathlib.Path) -> "ThrottleConfig":
        """Load + parse the yaml at ``path``.

        File missing → defaults (warn once at construction; nothing
        here). Parse error → defaults with a warning so a malformed
        edit doesn't take cozylobe offline.
        """
        import yaml  # local import; keeps cold-start light

        if not path.is_file():
            return cls.default()
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            log.warning(
                "cozylobe throttle: failed to parse %s: %s; using defaults",
                path,
                exc,
            )
            return cls.default()
        if raw is None:
            return cls.default()
        return cls.from_mapping(raw)


@dataclass(frozen=True)
class ThrottleDecision:
    """Result of :meth:`Throttle.handle`.

    ``action`` is one of:

    * ``"pass"`` — caller proceeds with the original event.
    * ``"drop"`` — caller skips the event entirely.
    * ``"summary"`` — caller proceeds with ``event``, which is a
      synthesized summary that carries the entity's current state
      plus a ``_suppressed_count`` field.

    ``reason`` is a short string for telemetry / debugging
    (``"state_transition"``, ``"micro_delta"``, ``"summary_window"``,
    ``"unknown_kind"``, ``"override"``, ``"first_seen"``).
    """

    action: str
    event: CozyHemEvent
    reason: str = ""


# Summary events use a distinct kind so downstream consumers (thinking,
# vault grooming) can spot them in the dailies if needed.
SUMMARY_EVENT_KIND = "entity:update_summary"


class Throttle:
    """Diff-aware filter sitting between sse_consumer and the wake loop.

    Thread-safety: the cozylobe wake loop drains its queue serially
    on a single asyncio task, so no internal locking is needed. If a
    future change parallelizes the drain, wrap state mutations in a
    lock — until then we lean on the single-consumer invariant.
    """

    def __init__(
        self,
        config_path: pathlib.Path = DEFAULT_CONFIG_PATH,
        *,
        clock: Optional[Any] = None,
    ) -> None:
        self._config_path = pathlib.Path(config_path)
        self._clock = clock or time.time
        self._config = ThrottleConfig.default()
        self._config_mtime: float = 0.0
        # (entity_id) -> last emitted payload dict. We compare against
        # the last EMITTED value (not last raw) so a slow ramp still
        # eventually trips the threshold instead of drifting unseen.
        self._last_emitted: dict[str, dict] = {}
        # (entity_id) -> wall-clock timestamp of the last summary we
        # emitted for that entity. Used to decide when the coalesce
        # window has elapsed.
        self._last_summary_at: dict[str, float] = {}
        # (entity_id) -> count of suppressed events since the last
        # summary. Embedded into the next summary event so thinking can
        # see the rate.
        self._pending_counts: dict[str, int] = {}
        # Load initial config (mtime-zero so the first event always
        # picks up a freshly-edited file on its initial pass).
        self._maybe_reload()

    @property
    def config(self) -> ThrottleConfig:
        """Expose the current parsed config (mainly for tests)."""
        return self._config

    def _maybe_reload(self) -> None:
        """Stat the yaml; if mtime changed, re-parse.

        Cheap enough to run per event (one ``stat`` call). Sidesteps
        the inotify dependency and works fine across container bind
        mounts where inotify is flaky.
        """
        try:
            mtime = self._config_path.stat().st_mtime
        except FileNotFoundError:
            # File missing — keep current config; nothing to reload.
            return
        except OSError as exc:
            log.warning(
                "cozylobe throttle: stat(%s) failed: %s; keeping current config",
                self._config_path,
                exc,
            )
            return
        if mtime == self._config_mtime:
            return
        new_cfg = ThrottleConfig.load(self._config_path)
        self._config = new_cfg
        self._config_mtime = mtime
        log.info(
            "cozylobe throttle: reloaded config from %s "
            "(coalesce_window_s=%.1f, tracked=%s, overrides=%d)",
            self._config_path,
            new_cfg.coalesce_window_s,
            sorted(new_cfg.tracked_numeric_fields),
            len(new_cfg.always_pass_entities),
        )

    def is_input_kind(self, event: CozyHemEvent) -> bool:
        """Return True iff ``event.kind`` is on the INPUT_KINDS allowlist.

        Triggers a config-mtime reload first so a runtime edit to the
        yaml takes effect on the next event without a daemon restart.
        An empty allowlist is treated as "accept all" (fail-open) so a
        botched config can't take cozylobe offline silently.

        This is the primary filter applied at the SSE entry point by
        :class:`alice_cozylobe.wake_loop.WakeLoop`. OUTPUT events
        (circadian brightness deltas, propagated light states, automation
        setpoint writes) fall on the wrong side of this gate and are
        dropped before any throttle, classify, or note write. See
        Phase 2 of #379 and the motion-cortex design §1.1.
        """
        self._maybe_reload()
        if not self._config.input_kinds:
            return True
        return event.kind in self._config.input_kinds

    def handle(self, event: CozyHemEvent) -> ThrottleDecision:
        """Apply the throttle rules to one event.

        See module docstring for the rule set. Fail-open on anything
        unusual — better to over-classify than to silently drop an
        event we should have surfaced.
        """
        self._maybe_reload()

        # Fail-open #1: anything that's not entity:update passes
        # through. Motion, doorbell, scene changes, novel kinds.
        if event.kind != _ENTITY_UPDATE_KIND:
            return ThrottleDecision("pass", event, reason="unknown_kind")

        # Fail-open #2: entity:update without an entity_id is a
        # producer bug; let the wake loop handle it.
        if not event.entity_id:
            return ThrottleDecision("pass", event, reason="no_entity_id")

        # Fail-open #3: per-entity override pins.
        if event.entity_id in self._config.always_pass_entities:
            self._record_emit(event)
            return ThrottleDecision("pass", event, reason="override")

        # Fail-open #4: per-kind override (covers kinds someone might
        # rename or alias into entity:update via a future producer).
        if event.kind in self._config.always_pass_kinds:
            self._record_emit(event)
            return ThrottleDecision("pass", event, reason="override")

        last = self._last_emitted.get(event.entity_id)
        if last is None:
            # First time we've seen this entity — let it through so
            # subsequent diffs have a baseline. This is also why
            # cold-start traffic is briefly noisy and then settles.
            self._record_emit(event)
            return ThrottleDecision("pass", event, reason="first_seen")

        # State-field transitions ALWAYS pass. We check this before
        # the micro-delta logic so an on→off doesn't get treated as
        # a brightness diff.
        if self._is_state_transition(last, event.payload):
            self._record_emit(event)
            return ThrottleDecision("pass", event, reason="state_transition")

        # Now the routine-delta check. If every changing field is
        # tracked-and-within-threshold, this is a candidate for
        # suppression. If any changing field is non-tracked (e.g.
        # the entity reports a new attribute we don't have a rule
        # for), pass — fail-open.
        if not self._all_changes_are_micro_deltas(last, event.payload):
            self._record_emit(event)
            return ThrottleDecision("pass", event, reason="non_micro_change")

        # Micro-delta. Check whether the coalesce window has elapsed
        # for this entity. If so, emit a summary; otherwise drop.
        now = self._clock()
        last_summary = self._last_summary_at.get(event.entity_id, 0.0)
        self._pending_counts[event.entity_id] = (
            self._pending_counts.get(event.entity_id, 0) + 1
        )
        if now - last_summary >= self._config.coalesce_window_s:
            suppressed = self._pending_counts[event.entity_id]
            self._pending_counts[event.entity_id] = 0
            self._last_summary_at[event.entity_id] = now
            # Track the entity's REAL state (not the summary
            # metadata) as last_emitted so the next diff sees a
            # proper baseline. If we stored the summary's enriched
            # payload here, the next event would see _summary /
            # _suppressed_count as "removed fields" and fail-open.
            self._last_emitted[event.entity_id] = dict(event.payload)
            summary = self._build_summary(event, suppressed)
            return ThrottleDecision(
                "summary", summary, reason="summary_window"
            )

        # Inside the window: drop. The next pass on this entity that
        # crosses the window will pick up the count.
        return ThrottleDecision("drop", event, reason="micro_delta")

    # ------------------------------------------------------------------
    # internals

    def _record_emit(self, event: CozyHemEvent) -> None:
        """Update last_emitted + reset pending counter for an entity
        passthrough.

        Called on every PASS decision. NOT called on summaries (the
        summary path inlines its own state update so the internal
        ``_summary`` / ``_suppressed_count`` metadata never lands in
        last_emitted), and NOT called on drops (the whole point of
        last_emitted is that it tracks the last value we let through,
        so a slow ramp eventually trips the threshold instead of
        drifting unseen).
        """
        self._last_emitted[event.entity_id] = dict(event.payload)
        self._pending_counts.pop(event.entity_id, None)
        self._last_summary_at[event.entity_id] = self._clock()

    @staticmethod
    def _is_state_transition(last: dict, new: dict) -> bool:
        """Did the ``state`` field change between last-emitted and now?

        We treat ``state`` as the canonical on/off field. Any string
        change qualifies (``"on"``→``"off"``, ``"off"``→``"on"``,
        ``"unavailable"``→``"on"``, etc.). If neither side has the
        field, returns False.
        """
        if "state" not in new:
            return False
        return last.get("state") != new.get("state")

    def _all_changes_are_micro_deltas(
        self, last: dict, new: dict
    ) -> bool:
        """True iff every field that changed between ``last`` and ``new``
        is in ``tracked_numeric_fields`` and changed by less than its
        configured threshold.

        Empty payload → False (treat as a real event; fail-open).
        Non-numeric tracked field → False (likely a producer change;
        let qwen see it). Any non-tracked field that changed → False.
        """
        if not new:
            return False

        tracked = self._config.tracked_numeric_fields
        any_micro_delta_field_seen = False
        # Compare on the union of keys so a removed field still counts
        # as a change.
        all_keys = set(last) | set(new)
        for key in all_keys:
            old_val = last.get(key)
            new_val = new.get(key)
            if old_val == new_val:
                continue  # unchanged
            if key not in tracked:
                return False  # untracked field changed → pass
            # Tracked field changed — must be numeric and within
            # threshold to count as a micro-delta.
            if not isinstance(old_val, (int, float)) or not isinstance(
                new_val, (int, float)
            ):
                return False
            threshold = tracked[key]
            if abs(new_val - old_val) >= threshold:
                return False
            any_micro_delta_field_seen = True

        return any_micro_delta_field_seen

    def _build_summary(
        self, event: CozyHemEvent, suppressed_count: int
    ) -> CozyHemEvent:
        """Synthesize the summary event handed to the wake loop.

        Carries the entity's current state plus metadata so the
        downstream classify/agent path can render a one-liner like
        "office lamp drifted from 0.14 to 0.07 over 47 micro-deltas in
        the last 5 minutes." We don't run qwen here — the wake loop
        owns that.
        """
        return CozyHemEvent(
            kind=SUMMARY_EVENT_KIND,
            entity_id=event.entity_id,
            payload={
                **event.payload,
                "_summary": True,
                "_suppressed_count": suppressed_count,
                "_window_seconds": self._config.coalesce_window_s,
                "_origin_kind": event.kind,
            },
            received_at=event.received_at,
        )


def ensure_user_config(
    user_path: pathlib.Path = DEFAULT_CONFIG_PATH,
    shipped_path: pathlib.Path = DEFAULT_SHIPPED_CONFIG_PATH,
) -> bool:
    """Bootstrap the user-editable config from the shipped default.

    Called by the daemon at startup. If ``user_path`` doesn't exist,
    copy ``shipped_path`` to it (creating parents as needed). Returns
    ``True`` if a copy happened, ``False`` otherwise. Idempotent: a
    second call on the same machine is a no-op.

    Failures are logged but do NOT raise — the throttle falls back to
    code defaults so a broken bootstrap can't take cozylobe offline.
    """
    if user_path.exists():
        return False
    if not shipped_path.is_file():
        log.warning(
            "cozylobe throttle: shipped default not found at %s; "
            "skipping bootstrap (throttle will use code defaults)",
            shipped_path,
        )
        return False
    try:
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(shipped_path.read_text())
    except OSError as exc:
        log.warning(
            "cozylobe throttle: failed to seed user config at %s: %s",
            user_path,
            exc,
        )
        return False
    log.info(
        "cozylobe throttle: seeded user config %s from shipped default",
        user_path,
    )
    return True
