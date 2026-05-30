"""Unit tests for the cozylobe_cortex statistical classifier (Phase 5 of #399).

Covers:

* :func:`classify_from_trail` with no profiles → returns ``person_id=None``.
* :func:`classify_from_trail` with a seeded profile that uniquely matches the
  trail → returns the matching person.
* Motion-pipeline integration: when the classifier raises on import or call,
  :meth:`MotionPipeline._run_statistical_classifier` returns ``None`` and the
  pipeline keeps going.
* :meth:`MotionPipeline._emit_guess` enriches the persisted guess's
  ``person`` + ``confidence`` when classification confidence is above the
  override threshold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from alice_cozylobe.guesses import GuessLifecycle
from alice_cozylobe.motion import (
    STATISTICAL_CLASSIFIER_OVERRIDE_THRESHOLD,
    MotionEvent,
    MotionInference,
    MotionPipeline,
)
from cozylobe_cortex.classify import (
    BehavioralProfile,
    ClassificationResult,
    MotionEvent as ClassifyMotionEvent,
    classify_from_trail,
)


# ---------------------------------------------------------------------------
# classify_from_trail() unit tests
# ---------------------------------------------------------------------------


def _make_classify_events(rooms: list[str], start_hour: int = 9) -> list[ClassifyMotionEvent]:
    """Build a stub trail in the classify dataclass shape."""
    base = datetime(2026, 5, 26, start_hour, 0, 0, tzinfo=timezone.utc)
    out: list[ClassifyMotionEvent] = []
    for i, room in enumerate(rooms):
        out.append(
            ClassifyMotionEvent(
                entity_id=f"binary_sensor.{room.lower()}_motion",
                room=room,
                ts=base + timedelta(seconds=30 * i),
                state="on",
            )
        )
    return out


def test_classify_from_trail_with_no_profiles_returns_none():
    """No profiles in scope → classifier returns person_id=None."""
    events = _make_classify_events(["Kitchen", "Hallway"])
    result = classify_from_trail(events, profiles={})
    assert isinstance(result, ClassificationResult)
    assert result.person_id is None
    assert result.confidence == 0.0


def test_classify_from_trail_with_empty_trail_returns_none():
    """Empty trail → person_id=None even when profiles exist."""
    profiles = {
        "Jason": BehavioralProfile(
            name="Jason",
            room_preferences={"Kitchen": 0.5, "Office": 0.5},
        ),
    }
    result = classify_from_trail([], profiles=profiles)
    assert result.person_id is None
    assert result.confidence == 0.0


def test_classify_from_trail_with_seeded_profile_matches_person():
    """A trail that lives entirely in Jason's preferred rooms (and not
    Katie's) should classify as Jason."""
    profiles = {
        "Jason": BehavioralProfile(
            name="Jason",
            # Jason's preferences strongly cover Office + Gym.
            room_preferences={"Office": 0.6, "Gym": 0.3, "Kitchen": 0.1},
        ),
        "Katie": BehavioralProfile(
            name="Katie",
            # Katie's are uniform across rooms Jason doesn't visit much.
            room_preferences={"Living Room": 0.5, "Master Bathroom": 0.5},
        ),
    }
    events = _make_classify_events(["Office", "Gym", "Office", "Gym"])
    result = classify_from_trail(events, profiles=profiles)
    assert result.person_id == "Jason"
    assert result.confidence > 0.65


def test_classify_from_trail_returns_none_when_profiles_have_no_behavioral_data():
    """Profiles exist but carry no room_preferences / tod / transitions →
    classifier abstains rather than guessing uniformly."""
    profiles = {
        "Jason": BehavioralProfile(name="Jason"),
        "Katie": BehavioralProfile(name="Katie"),
    }
    events = _make_classify_events(["Kitchen", "Hallway"])
    result = classify_from_trail(events, profiles=profiles)
    assert result.person_id is None
    assert result.confidence == 0.0


def test_classify_from_trail_respects_n_window():
    """When ``n`` is smaller than the input, only the first N events feed
    the classifier (consistent with build_trail's behavior)."""
    profiles = {
        "Jason": BehavioralProfile(
            name="Jason",
            room_preferences={"Office": 1.0},
        ),
    }
    # 24 events but n=2 — function should still complete cleanly.
    events = _make_classify_events(["Office"] * 24)
    result = classify_from_trail(events, profiles=profiles, n=2)
    # Only one room visited in the truncated window → still recognisable.
    assert result.person_id == "Jason"


# ---------------------------------------------------------------------------
# MotionPipeline._run_statistical_classifier failure handling
# ---------------------------------------------------------------------------


class _StubQwen:
    """Stub qwen client returning a canned positional inference."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> dict:
        self.calls.append(prompt)
        return {
            "current_room": "Kitchen",
            "confidence": 0.5,
            "person_hypothesis": "unknown",
            "person_confidence": 0.2,
            "next_room_hypothesis": "Hallway",
            "next_room_confidence": 0.3,
            "reasoning": "stub",
        }


def _motion(
    room: str = "Kitchen",
    *,
    entity_id: str = "hue_kitchen_motion",
    timestamp: float = 1_000.0,
    state: str = "on",
) -> MotionEvent:
    return MotionEvent(
        timestamp=timestamp,
        entity_id=entity_id,
        state=state,
        room_id=room,
    )


def test_run_statistical_classifier_returns_none_on_empty_trail():
    """An empty trail (or trail with no resolved rooms) → None,
    not an exception. The pipeline degrades gracefully."""
    pipeline = MotionPipeline(llm_client=_StubQwen())
    # Trail is empty by default — classifier should bail without error.
    assert pipeline._run_statistical_classifier() is None


def test_run_statistical_classifier_skips_events_without_room():
    """Events whose ``room_id`` is None get dropped before the call; if
    all events in the trail have no room, returns None."""
    pipeline = MotionPipeline(llm_client=_StubQwen())
    pipeline.trail.append(
        MotionEvent(
            timestamp=1_000.0,
            entity_id="hue_unknown_motion",
            state="on",
            room_id=None,
        )
    )
    assert pipeline._run_statistical_classifier() is None


def test_run_statistical_classifier_swallows_import_failure(monkeypatch):
    """If cozylobe_cortex import raises (module-level failure), the
    method must swallow the exception and return None."""
    import builtins

    real_import = builtins.__import__

    def _explode(name, *args, **kwargs):
        if name == "cozylobe_cortex.classify":
            raise ImportError("simulated classify.py import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _explode)

    pipeline = MotionPipeline(llm_client=_StubQwen())
    pipeline.trail.append(_motion(room="Kitchen", timestamp=1_000.0))
    pipeline.trail.append(_motion(room="Hallway", timestamp=1_030.0))
    # Must not raise; must not crash the pipeline.
    assert pipeline._run_statistical_classifier() is None


def test_run_statistical_classifier_swallows_runtime_exception(monkeypatch):
    """If classify_from_trail itself raises at call time, swallow."""
    import sys

    # ``cozylobe_cortex.classify`` the *attribute* on the package resolves
    # to the re-exported function (see ``cozylobe_cortex/__init__.py``),
    # not the submodule — so go through ``sys.modules`` to patch the
    # actual module the pipeline imports from.
    classify_mod = sys.modules["cozylobe_cortex.classify"]

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated classifier blow-up")

    monkeypatch.setattr(classify_mod, "classify_from_trail", _boom)

    pipeline = MotionPipeline(llm_client=_StubQwen())
    pipeline.trail.append(_motion(room="Kitchen", timestamp=1_000.0))
    pipeline.trail.append(_motion(room="Hallway", timestamp=1_030.0))
    assert pipeline._run_statistical_classifier() is None


# ---------------------------------------------------------------------------
# Guess enrichment in _emit_guess
# ---------------------------------------------------------------------------


def _make_inference(
    person: Optional[str] = "unknown",
    person_conf: Optional[float] = 0.2,
) -> MotionInference:
    return MotionInference(
        current_room="Kitchen",
        confidence=0.5,
        person_hypothesis=person,
        person_confidence=person_conf,
        next_room_hypothesis="Hallway",
        next_room_confidence=0.3,
        reasoning="stub",
        raw={},
    )


def _make_pipeline_with_vault(tmp_path: Path) -> MotionPipeline:
    """Build a pipeline that writes guesses to ``tmp_path`` so
    ``_emit_guess`` actually fires."""
    return MotionPipeline(
        llm_client=_StubQwen(),
        vault_root=tmp_path,
        lifecycle=GuessLifecycle(vault_root=tmp_path),
    )


def test_emit_guess_applies_classifier_override_when_confident(tmp_path: Path):
    """High-confidence classification → guess.person overridden + confidence
    bumped to max."""
    pipeline = _make_pipeline_with_vault(tmp_path)
    batch = [_motion(room="Kitchen", timestamp=1_000.0)]
    inference = _make_inference(person="unknown", person_conf=0.2)
    classification = ClassificationResult(
        person_id="Jason",
        confidence=0.9,
        scores={"Jason": 0.9, "Katie": 0.1},
    )
    guess = pipeline._emit_guess(batch, inference, classification=classification)
    assert guess is not None
    assert guess.person == "Jason"
    # Either inference.confidence (0.5) or classification.confidence (0.9)
    # — the override pushes the guess confidence to the higher value.
    assert guess.confidence >= 0.9


def test_emit_guess_does_not_override_below_threshold(tmp_path: Path):
    """Classification confidence at or below threshold → no override."""
    pipeline = _make_pipeline_with_vault(tmp_path)
    batch = [_motion(room="Kitchen", timestamp=1_000.0)]
    inference = _make_inference(person="Katie", person_conf=0.4)
    classification = ClassificationResult(
        person_id="Jason",
        # Equal to the threshold — strictly-greater-than gate must hold.
        confidence=STATISTICAL_CLASSIFIER_OVERRIDE_THRESHOLD,
        scores={},
    )
    guess = pipeline._emit_guess(batch, inference, classification=classification)
    assert guess is not None
    # Override didn't fire — qwen's person hypothesis stays.
    assert guess.person == "Katie"


def test_emit_guess_ignores_none_classification(tmp_path: Path):
    """When the classifier returned None (e.g., import failed), the
    qwen-derived guess passes through unchanged."""
    pipeline = _make_pipeline_with_vault(tmp_path)
    batch = [_motion(room="Kitchen", timestamp=1_000.0)]
    inference = _make_inference(person="Katie", person_conf=0.4)
    guess = pipeline._emit_guess(batch, inference, classification=None)
    assert guess is not None
    assert guess.person == "Katie"


def test_emit_guess_ignores_classification_without_person_id(tmp_path: Path):
    """ClassificationResult with person_id=None (abstained) must not
    overwrite the qwen-derived person, even if the abstain "confidence"
    field happens to read high."""
    pipeline = _make_pipeline_with_vault(tmp_path)
    batch = [_motion(room="Kitchen", timestamp=1_000.0)]
    inference = _make_inference(person="Katie", person_conf=0.4)
    classification = ClassificationResult(
        person_id=None,
        confidence=0.99,  # noise — shouldn't matter, gate keys on person_id
        scores={},
    )
    guess = pipeline._emit_guess(batch, inference, classification=classification)
    assert guess is not None
    assert guess.person == "Katie"
