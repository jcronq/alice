"""Tests for the cozylobe guess lifecycle (Phase 3 of #380).

Covers the Phase 3 acceptance pieces from the prompt:

* ``write_guess`` + ``load_guess`` round-trip.
* ``find_recent_guesses`` filtering (person, room, since, status).
* ``expire_overdue_guesses`` marks the right ones.
* Implicit confirmation lifts confidence past the threshold.
* Self-evident confirmation: predicted room → next event in that room
  within 60s → confidence +0.2, status=confirmed.
* Self-evident refutation: predicted room → next event in different
  room → status=refuted, expires_at extended by 30 days.
* Explicit confirmation/refutation API.
* Surface threshold logic at the three boundaries.
* End-to-end: motion classify produces guess + surface tier.

The lifecycle tests use an injected clock so the 30-minute and 60-second
windows don't require real wall-clock waits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from alice_cozylobe.guesses import (
    DEFAULT_IMPLICIT_CONFIRMATION_MINUTES,
    DEFAULT_REFUTED_TTL,
    DEFAULT_SCRATCH_TTL,
    Evidence,
    Guess,
    GuessLifecycle,
    GuessStatus,
    expire_overdue_guesses,
    find_recent_guesses,
    guess_from_inference,
    load_guess,
    surface_threshold,
    write_guess,
)
from alice_cozylobe.motion import (
    MotionEvent,
    MotionInference,
    MotionPipeline,
)


# ---------------------------------------------------------------------------
# Helpers


def _ts(year: int = 2026, month: int = 5, day: int = 26, hour: int = 10, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _motion_event(
    *,
    entity_id: str = "binary_sensor.hue_kitchen_motion",
    room: Optional[str] = "Kitchen",
    timestamp: Optional[datetime] = None,
) -> MotionEvent:
    moment = timestamp or _ts()
    return MotionEvent(
        timestamp=moment.timestamp(),
        entity_id=entity_id,
        state="on",
        room_id=room,
    )


def _make_guess(
    *,
    person: Optional[str] = "Jason",
    room: Optional[str] = "Kitchen",
    next_room: Optional[str] = "Office",
    confidence: float = 0.5,
    created: Optional[datetime] = None,
    title: Optional[str] = None,
    status: GuessStatus = GuessStatus.PENDING,
) -> Guess:
    moment = created or _ts()
    return Guess(
        title=title or f"{person or 'unknown'} in {room or 'unknown'}",
        person=person,
        room=room,
        confidence=confidence,
        status=status,
        next_room_hypothesis=next_room,
        evidence=[Evidence(kind="motion", entity_id="binary_sensor.hue_kitchen_motion", ts=moment)],
        trail_window=4,
        created=moment,
        updated=moment,
        expires_at=moment + DEFAULT_SCRATCH_TTL,
    )


class _FixedClock:
    """Inject-able clock for the lifecycle. Returns whatever ``now`` is."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# ---------------------------------------------------------------------------
# write_guess + load_guess


def test_write_guess_round_trips_through_load_guess(tmp_path: Path):
    guess = _make_guess(confidence=0.62)
    path = write_guess(tmp_path, guess)

    assert path.exists()
    assert path.parent == tmp_path / "guesses"
    assert path.suffix == ".md"
    # Filename carries timestamp + person + room slugs.
    assert "jason" in path.name.lower()
    assert "kitchen" in path.name.lower()

    loaded = load_guess(path)
    assert loaded.title == guess.title
    assert loaded.person == "Jason"
    assert loaded.room == "Kitchen"
    assert loaded.confidence == pytest.approx(0.62, abs=1e-3)
    assert loaded.status == GuessStatus.PENDING
    assert loaded.next_room_hypothesis == "Office"
    assert loaded.trail_window == 4
    assert loaded.created == guess.created
    assert loaded.expires_at == guess.expires_at
    # Evidence list round-trips.
    assert len(loaded.evidence) == 1
    assert loaded.evidence[0].entity_id == "binary_sensor.hue_kitchen_motion"


def test_write_guess_atomic_via_tempfile_rename(tmp_path: Path):
    """write_guess shouldn't leave any .tmp-* siblings behind."""
    guess = _make_guess()
    write_guess(tmp_path, guess)
    siblings = list((tmp_path / "guesses").glob(".tmp-*"))
    assert siblings == []


def test_write_guess_update_in_place_reuses_path(tmp_path: Path):
    """Subsequent writes for the same Guess overwrite the same file
    (lifecycle status updates must not litter)."""
    guess = _make_guess()
    first = write_guess(tmp_path, guess)
    guess.status = GuessStatus.CONFIRMED
    guess.confidence = 0.85
    second = write_guess(tmp_path, guess)
    assert first == second
    assert len(list((tmp_path / "guesses").glob("*.md"))) == 1
    reloaded = load_guess(second)
    assert reloaded.status == GuessStatus.CONFIRMED
    assert reloaded.confidence == pytest.approx(0.85, abs=1e-3)


def test_load_guess_tolerates_unknown_person(tmp_path: Path):
    """A guess written with person=None should round-trip as None."""
    guess = _make_guess(person=None, room=None)
    path = write_guess(tmp_path, guess)
    loaded = load_guess(path)
    assert loaded.person is None
    assert loaded.room is None


# ---------------------------------------------------------------------------
# find_recent_guesses


def test_find_recent_guesses_filters_by_person(tmp_path: Path):
    write_guess(tmp_path, _make_guess(person="Jason", room="Kitchen", created=_ts(hour=8)))
    write_guess(tmp_path, _make_guess(person="Katie", room="Bedroom", created=_ts(hour=9)))
    out = find_recent_guesses(tmp_path, person="Jason")
    assert len(out) == 1
    assert out[0].person == "Jason"


def test_find_recent_guesses_filters_by_room(tmp_path: Path):
    write_guess(tmp_path, _make_guess(person="Jason", room="Kitchen", created=_ts(hour=8)))
    write_guess(tmp_path, _make_guess(person="Jason", room="Office", created=_ts(hour=9)))
    out = find_recent_guesses(tmp_path, room="Office")
    assert len(out) == 1
    assert out[0].room == "Office"


def test_find_recent_guesses_filters_by_since(tmp_path: Path):
    write_guess(tmp_path, _make_guess(created=_ts(hour=8)))
    write_guess(tmp_path, _make_guess(person="Katie", room="Bedroom", created=_ts(hour=10)))
    out = find_recent_guesses(tmp_path, since=_ts(hour=9))
    assert len(out) == 1
    assert out[0].created.hour == 10


def test_find_recent_guesses_filters_by_status(tmp_path: Path):
    write_guess(tmp_path, _make_guess(status=GuessStatus.PENDING, created=_ts(hour=8)))
    write_guess(tmp_path, _make_guess(person="Katie", room="Bedroom", status=GuessStatus.CONFIRMED, created=_ts(hour=9)))
    out = find_recent_guesses(tmp_path, status=GuessStatus.PENDING)
    assert len(out) == 1
    assert out[0].status == GuessStatus.PENDING


def test_find_recent_guesses_empty_vault_returns_empty(tmp_path: Path):
    assert find_recent_guesses(tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# expire_overdue_guesses


def test_expire_overdue_guesses_marks_only_past_due(tmp_path: Path):
    # One guess expires at 09:00, another at 11:00.
    old = _make_guess(person="Jason", room="Kitchen", created=_ts(hour=8))
    young = _make_guess(person="Katie", room="Bedroom", created=_ts(hour=10))
    write_guess(tmp_path, old)
    write_guess(tmp_path, young)

    # At 09:01 the 8am guess is overdue (24h TTL → expires at 8am next
    # day) — adjust by checking at a far-future ts.
    now = _ts(hour=8) + DEFAULT_SCRATCH_TTL + timedelta(minutes=1)
    count = expire_overdue_guesses(tmp_path, now)
    assert count == 1

    # The expired guess is the old one.
    loaded = find_recent_guesses(tmp_path)
    by_person = {g.person: g for g in loaded}
    assert by_person["Jason"].status == GuessStatus.EXPIRED
    assert by_person["Katie"].status == GuessStatus.PENDING


def test_expire_overdue_guesses_is_idempotent(tmp_path: Path):
    write_guess(tmp_path, _make_guess(created=_ts(hour=8)))
    now = _ts(hour=8) + DEFAULT_SCRATCH_TTL + timedelta(minutes=1)
    first = expire_overdue_guesses(tmp_path, now)
    second = expire_overdue_guesses(tmp_path, now)
    assert first == 1
    assert second == 0


# ---------------------------------------------------------------------------
# Lifecycle — implicit confirmation


def test_implicit_confirmation_lifts_confidence_after_threshold(tmp_path: Path):
    """30 minutes elapse with no contradiction → +0.1 confidence."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(tmp_path, _make_guess(confidence=0.5, created=start))

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(minutes=DEFAULT_IMPLICIT_CONFIRMATION_MINUTES + 1))
    updated = lifecycle.tick()
    assert len(updated) == 1
    assert updated[0].confidence == pytest.approx(0.6, abs=1e-3)
    assert updated[0].implicit_confirmed is True


def test_implicit_confirmation_idempotent_across_ticks(tmp_path: Path):
    """A second tick doesn't keep adding 0.1 to the same guess."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(tmp_path, _make_guess(confidence=0.5, created=start))

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(minutes=DEFAULT_IMPLICIT_CONFIRMATION_MINUTES + 1))
    first = lifecycle.tick()
    second = lifecycle.tick()
    assert len(first) == 1
    assert len(second) == 0
    # Confidence stays at 0.6, not 0.7.
    loaded = find_recent_guesses(tmp_path)
    assert loaded[0].confidence == pytest.approx(0.6, abs=1e-3)


def test_implicit_confirmation_does_not_fire_before_threshold(tmp_path: Path):
    """Inside the 30-min window, tick is a no-op."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(tmp_path, _make_guess(confidence=0.5, created=start))

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(minutes=10))
    updated = lifecycle.tick()
    assert updated == []


def test_implicit_confirmation_skips_non_pending(tmp_path: Path):
    """Refuted/confirmed/expired guesses are not bumped."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(
        tmp_path,
        _make_guess(confidence=0.5, created=start, status=GuessStatus.CONFIRMED),
    )

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(minutes=DEFAULT_IMPLICIT_CONFIRMATION_MINUTES + 1))
    updated = lifecycle.tick()
    assert updated == []


# ---------------------------------------------------------------------------
# Lifecycle — self-evident confirmation/refutation


def test_self_evident_confirmation_predicted_room_within_60s(tmp_path: Path):
    """Predicted next_room=Dining → next event in Dining within 60s →
    confidence +0.2 and status=confirmed."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(
        tmp_path,
        _make_guess(
            person="Jason",
            room="Kitchen",
            next_room="Dining",
            confidence=0.6,
            created=start,
        ),
    )

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    # 30s later, motion fires in Dining → confirm.
    clock.advance(timedelta(seconds=30))
    event = _motion_event(
        entity_id="binary_sensor.hue_dining_motion",
        room="Dining",
        timestamp=clock.now,
    )
    updated = lifecycle.process_new_event(event)
    assert len(updated) == 1
    assert updated[0].status == GuessStatus.CONFIRMED
    assert updated[0].confidence == pytest.approx(0.8, abs=1e-3)


def test_self_evident_refutation_different_room(tmp_path: Path):
    """Predicted next_room=Dining → next event in Nursery → refuted."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(
        tmp_path,
        _make_guess(
            person="Jason",
            room="Kitchen",
            next_room="Dining",
            confidence=0.6,
            created=start,
        ),
    )

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(seconds=30))
    event = _motion_event(
        entity_id="binary_sensor.hue_nursery_motion",
        room="Nursery",
        timestamp=clock.now,
    )
    updated = lifecycle.process_new_event(event)
    assert len(updated) == 1
    assert updated[0].status == GuessStatus.REFUTED
    # Refuted guesses get a 30-day extended TTL for pattern analysis.
    expected_expires = clock.now + DEFAULT_REFUTED_TTL
    assert updated[0].expires_at == expected_expires


def test_self_evident_same_room_is_neutral(tmp_path: Path):
    """A follow-up event in the SAME room as the guess (person didn't
    move yet) neither confirms nor refutes."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(
        tmp_path,
        _make_guess(
            person="Jason",
            room="Kitchen",
            next_room="Dining",
            confidence=0.6,
            created=start,
        ),
    )

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(seconds=20))
    event = _motion_event(room="Kitchen", timestamp=clock.now)
    updated = lifecycle.process_new_event(event)
    assert updated == []
    # Still pending, confidence unchanged.
    loaded = find_recent_guesses(tmp_path)
    assert loaded[0].status == GuessStatus.PENDING
    assert loaded[0].confidence == pytest.approx(0.6, abs=1e-3)


def test_self_evident_window_expires_after_60s(tmp_path: Path):
    """Past 60s the predicted-room event no longer counts as
    self-evident — confirmation must come from implicit or explicit."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(
        tmp_path,
        _make_guess(
            person="Jason",
            room="Kitchen",
            next_room="Dining",
            confidence=0.6,
            created=start,
        ),
    )

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(seconds=120))
    event = _motion_event(
        entity_id="binary_sensor.hue_dining_motion",
        room="Dining",
        timestamp=clock.now,
    )
    updated = lifecycle.process_new_event(event)
    assert updated == []


def test_self_evident_event_without_room_is_a_noop(tmp_path: Path):
    """If the new motion event has no resolved room (e.g. unknown
    sensor), the lifecycle can't decide — return empty."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    write_guess(
        tmp_path,
        _make_guess(next_room="Dining", confidence=0.6, created=start),
    )
    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    clock.advance(timedelta(seconds=10))
    event = _motion_event(room=None, timestamp=clock.now)
    assert lifecycle.process_new_event(event) == []


# ---------------------------------------------------------------------------
# Lifecycle — explicit confirmation/refutation


def test_apply_explicit_confirmation_sets_confidence_to_one(tmp_path: Path):
    start = _ts(hour=10)
    clock = _FixedClock(start)
    path = write_guess(tmp_path, _make_guess(confidence=0.5, created=start))

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    result = lifecycle.apply_explicit_confirmation(path.stem, by="jason")
    assert result is not None
    assert result.confidence == pytest.approx(1.0, abs=1e-3)
    assert result.status == GuessStatus.CONFIRMED
    # An evidence entry was appended carrying "jason" as the source.
    explicit = [e for e in result.evidence if e.kind == "explicit_confirmation"]
    assert len(explicit) == 1
    assert explicit[0].entity_id == "jason"


def test_apply_explicit_confirmation_allows_person_correction(tmp_path: Path):
    """Jason might say 'no that was Katie' — the API should accept
    a person override that flips the identity field."""
    start = _ts(hour=10)
    clock = _FixedClock(start)
    path = write_guess(
        tmp_path,
        _make_guess(person="Jason", confidence=0.5, created=start),
    )
    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    result = lifecycle.apply_explicit_confirmation(path.stem, person="Katie")
    assert result is not None
    assert result.person == "Katie"
    # Round-trip through disk to confirm persistence.
    reloaded = load_guess(path)
    assert reloaded.person == "Katie"


def test_apply_explicit_refutation_extends_expiry_by_30d(tmp_path: Path):
    start = _ts(hour=10)
    clock = _FixedClock(start)
    path = write_guess(tmp_path, _make_guess(confidence=0.7, created=start))

    lifecycle = GuessLifecycle(tmp_path, clock=clock)
    result = lifecycle.apply_explicit_refutation(path.stem, "wrong-person")
    assert result is not None
    assert result.status == GuessStatus.REFUTED
    expected = clock.now + DEFAULT_REFUTED_TTL
    assert result.expires_at == expected


def test_apply_explicit_to_unknown_id_returns_none(tmp_path: Path):
    (tmp_path / "guesses").mkdir()
    lifecycle = GuessLifecycle(tmp_path)
    assert lifecycle.apply_explicit_confirmation("does-not-exist") is None
    assert lifecycle.apply_explicit_refutation("does-not-exist", "reason") is None


# ---------------------------------------------------------------------------
# Surface threshold


def test_surface_threshold_silent_below_03():
    guess = _make_guess(confidence=0.2)
    assert surface_threshold(guess) == "silent"


def test_surface_threshold_log_in_middle_band():
    """0.3 ≤ confidence < 0.7 → log tier (with or without unexpected)."""
    guess = _make_guess(confidence=0.5)
    assert surface_threshold(guess) == "log"
    assert surface_threshold(guess, unexpected=True) == "log"


def test_surface_threshold_actionable_high_conf_and_unexpected():
    """Both gates must trigger: high confidence AND unexpected."""
    guess = _make_guess(confidence=0.85)
    assert surface_threshold(guess, unexpected=True) == "actionable"
    # High confidence alone is just routine — log tier.
    assert surface_threshold(guess, unexpected=False) == "log"


def test_surface_threshold_at_exact_boundaries():
    """0.3 is log (>=), 0.7 is actionable when unexpected."""
    low = _make_guess(confidence=0.3)
    high = _make_guess(confidence=0.7)
    assert surface_threshold(low) == "log"
    assert surface_threshold(high, unexpected=True) == "actionable"


def test_surface_threshold_silent_when_no_room_and_no_next_room():
    """No actionable inference = silent even when confidence is high."""
    g = Guess(
        title="empty inference",
        person="Jason",
        room=None,
        confidence=0.9,
        next_room_hypothesis=None,
        created=_ts(),
    )
    assert surface_threshold(g, unexpected=True) == "silent"


# ---------------------------------------------------------------------------
# guess_from_inference factory


def test_guess_from_inference_carries_fields():
    inf = MotionInference(
        current_room="Kitchen",
        confidence=0.65,
        person_hypothesis="Jason",
        person_confidence=0.5,
        next_room_hypothesis="Office",
        next_room_confidence=0.4,
        reasoning="trail says Bedroom→Kitchen, Office is morning routine.",
        raw={},
    )
    batch = [
        MotionEvent(
            timestamp=_ts(hour=10, minute=15).timestamp(),
            entity_id="binary_sensor.hue_kitchen_motion",
            state="on",
            room_id="Kitchen",
        )
    ]
    guess = guess_from_inference(inf, batch, trail_window=12, now=_ts(hour=10, minute=15))
    assert guess.person == "Jason"
    assert guess.room == "Kitchen"
    assert guess.confidence == pytest.approx(0.65, abs=1e-3)
    assert guess.next_room_hypothesis == "Office"
    assert guess.next_room_confidence == pytest.approx(0.4, abs=1e-3)
    assert guess.trail_window == 12
    assert len(guess.evidence) == 1
    assert guess.evidence[0].entity_id == "binary_sensor.hue_kitchen_motion"
    # Title carries the time.
    assert "10:15" in guess.title
    # Body carries the IMPLIES edge so the relation is queryable.
    assert "(IMPLIES:" in guess.body
    assert "[[rooms/Office]]" in guess.body


def test_guess_from_inference_unknown_person_renders_as_unknown(tmp_path: Path):
    inf = MotionInference(
        current_room="Kitchen",
        confidence=0.4,
        person_hypothesis=None,
        person_confidence=0.0,
        next_room_hypothesis=None,
        next_room_confidence=None,
        reasoning="no person signal yet",
        raw={},
    )
    guess = guess_from_inference(inf, [], now=_ts())
    assert guess.person is None
    assert "unknown" in guess.title.lower()
    # Round-trip through disk so the frontmatter handling is exercised.
    path = write_guess(tmp_path, guess)
    reloaded = load_guess(path)
    assert reloaded.person is None


# ---------------------------------------------------------------------------
# End-to-end: motion pipeline emits guess + surface tier


def _cozyhem_event(
    kind: str = "motion_detected",
    entity_id: str = "binary_sensor.hue_kitchen_motion",
    *,
    received_at: float = 1_000.0,
):
    from alice_cozylobe.events import CozyHemEvent

    return CozyHemEvent(
        kind=kind,
        entity_id=entity_id,
        payload={"state": "on"},
        received_at=received_at,
    )


class _StubQwen:
    def __init__(self, payload=None) -> None:
        self.payload = payload or {
            "current_room": "Kitchen",
            "confidence": 0.85,
            "person_hypothesis": "Jason",
            "person_confidence": 0.7,
            "next_room_hypothesis": "Office",
            "next_room_confidence": 0.6,
            "reasoning": "Bedroom motion at 10:10, kitchen at 10:15.",
        }
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> dict:
        self.calls.append(prompt)
        return self.payload


@pytest.mark.asyncio
async def test_motion_pipeline_writes_guess_to_vault_root(tmp_path: Path):
    """A successful classify should land a guess note in
    ``vault_root/guesses/`` alongside the observation note."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        vault_root=tmp_path,
        write_note=_capture,
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem_event())

    # Observation note still went out.
    assert len(notes) == 1
    # And a guess landed in the vault.
    files = list((tmp_path / "guesses").glob("*.md"))
    assert len(files) == 1
    loaded = load_guess(files[0])
    assert loaded.person == "Jason"
    assert loaded.room == "Kitchen"
    assert loaded.confidence == pytest.approx(0.85, abs=1e-3)
    assert loaded.next_room_hypothesis == "Office"


@pytest.mark.asyncio
async def test_motion_pipeline_emits_actionable_surface_for_security_high_conf(
    tmp_path: Path,
):
    """When the classify produces confidence ≥ 0.7 AND the event is
    security-class (unexpected), the pipeline should drop a surface
    file via ``write_urgent_surface``."""
    qwen = _StubQwen()  # default confidence 0.85
    notes: list[dict] = []
    surfaces: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    def _capture_surface(body, *, slug, surface_type, extra_frontmatter=None):
        surfaces.append(
            {
                "body": body,
                "slug": slug,
                "surface_type": surface_type,
                "extra_frontmatter": extra_frontmatter or {},
            }
        )
        return Path("/tmp/fake-surface.md")

    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        vault_root=tmp_path,
        write_note=_capture,
        write_surface=_capture_surface,
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem_event())

    assert len(surfaces) == 1
    assert surfaces[0]["surface_type"] == "cozylobe-actionable"
    # Surface frontmatter carries the guess id + confidence so the
    # speaking daemon can dedupe + route.
    assert surfaces[0]["extra_frontmatter"]["person"] == "Jason"
    assert surfaces[0]["extra_frontmatter"]["room"] == "Kitchen"
    assert surfaces[0]["extra_frontmatter"]["security"] == "true"


@pytest.mark.asyncio
async def test_motion_pipeline_silent_low_confidence_no_surface(tmp_path: Path):
    """Confidence < 0.3 should be silent — observation note only, no
    surface file."""
    qwen = _StubQwen(
        payload={
            "current_room": "Kitchen",
            "confidence": 0.15,
            "person_hypothesis": "Jason",
            "person_confidence": 0.1,
            "next_room_hypothesis": None,
            "next_room_confidence": 0.0,
            "reasoning": "ambiguous trail",
        }
    )
    notes: list[dict] = []
    surfaces: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    def _capture_surface(body, *, slug, **kwargs):
        surfaces.append({"body": body, "slug": slug})
        return Path("/tmp/fake-surface.md")

    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        vault_root=tmp_path,
        write_note=_capture,
        write_surface=_capture_surface,
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem_event())

    assert len(notes) == 1
    # Silent tier on the tags.
    assert "surface-tier:silent" in notes[0]["tags"]
    assert surfaces == []


@pytest.mark.asyncio
async def test_motion_pipeline_runs_lifecycle_on_new_event(tmp_path: Path):
    """The pipeline should call lifecycle.process_new_event for every
    motion event so self-evident confirmation/refutation fires inside
    its 60-second window."""

    class _RecordingLifecycle:
        def __init__(self) -> None:
            self.calls: list[MotionEvent] = []

        def process_new_event(self, event: MotionEvent):
            self.calls.append(event)
            return []

    qwen = _StubQwen()
    lifecycle = _RecordingLifecycle()
    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        vault_root=tmp_path,
        lifecycle=lifecycle,
        write_note=lambda *a, **kw: Path("/tmp/x.md"),
        write_surface=lambda *a, **kw: Path("/tmp/y.md"),
        security_predicate=lambda _e: False,  # queue path
    )
    await pipeline.handle(_cozyhem_event())
    assert len(lifecycle.calls) == 1
    assert lifecycle.calls[0].entity_id == "binary_sensor.hue_kitchen_motion"


@pytest.mark.asyncio
async def test_motion_pipeline_without_vault_root_still_writes_note(tmp_path: Path):
    """Phase 2 compatibility: a pipeline with no vault_root configured
    should still emit the observation note (no guess, no surface)."""
    qwen = _StubQwen()
    notes: list[dict] = []

    def _capture(body, *, slug, tags):
        notes.append({"body": body, "slug": slug, "tags": tags})
        return Path("/tmp/fake-note.md")

    pipeline = MotionPipeline(
        llm_client=qwen,
        vault=None,
        # vault_root=None (default) → guess emission skipped
        write_note=_capture,
        security_predicate=lambda _e: True,
    )
    await pipeline.handle(_cozyhem_event())
    assert len(notes) == 1
    # No guess directory was created.
    assert not (tmp_path / "guesses").exists()
