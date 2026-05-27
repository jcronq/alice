"""Motion-cortex person identification pipeline.

Classifies motion trails against known people's behavioral profiles using
a Dirichlet-Multinomial model. Returns (person_id, confidence) for each trail.

Pure functions — no mutable state. Profile data read from vault notes on
each call.

**Relocated 2026-05-26 (Phase 5, #399).** This module used to live at
``~/alice-mind/cozylobe-cortex/classify.py``. It now lives in the alice
repo because it is code, not vault knowledge. The profile files
(``people/*.md``) stay in the alice-mind vault — only the classifier
moved. The profile-loading path is configurable via the
``COZYLOBE_CORTEX_PATH`` environment variable so the bind-mounted alice
container picks up the live vault.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MotionEvent:
    """A single motion sensor event."""
    entity_id: str
    room: str
    ts: datetime
    state: str  # "on" or "off"


@dataclass
class MotionTrail:
    """A window of recent motion events."""
    events: list[MotionEvent] = field(default_factory=list)

    @property
    def rooms_visited(self) -> set[str]:
        return {e.room for e in self.events}

    @property
    def room_duration(self) -> dict[str, float]:
        """Total time (seconds) spent in each room."""
        if len(self.events) < 2:
            return {}
        durations: dict[str, float] = {}
        sorted_events = sorted(self.events, key=lambda e: e.ts)
        for i in range(len(sorted_events) - 1):
            room = sorted_events[i].room
            delta = (sorted_events[i + 1].ts - sorted_events[i].ts).total_seconds()
            durations[room] = durations.get(room, 0) + max(delta, 0)
        return durations

    @property
    def transitions(self) -> list[tuple[str, str]]:
        """Ordered room→room transitions."""
        if len(self.events) < 2:
            return []
        sorted_events = sorted(self.events, key=lambda e: e.ts)
        trans = []
        for i in range(len(sorted_events) - 1):
            prev_room = sorted_events[i].room
            next_room = sorted_events[i + 1].room
            if prev_room != next_room:
                trans.append((prev_room, next_room))
        return trans

    @property
    def session_length(self) -> float:
        """Time span of trail in seconds."""
        if not self.events:
            return 0.0
        ts = [e.ts for e in self.events]
        return (max(ts) - min(ts)).total_seconds()

    @property
    def first_seen(self) -> datetime | None:
        if not self.events:
            return None
        return min(e.ts for e in self.events)

    @property
    def last_seen(self) -> datetime | None:
        if not self.events:
            return None
        return max(e.ts for e in self.events)


@dataclass
class BehavioralProfile:
    """Behavioral profile for a known person."""
    name: str
    room_preferences: dict[str, float] = field(default_factory=dict)
    time_of_day: dict[str, float] = field(default_factory=dict)
    common_transitions: list[tuple[str, str]] = field(default_factory=list)
    typical_session_length: float = 1800.0  # 30 min default
    session_std: float = 900.0  # 15 min std default
    last_updated: datetime | None = None


@dataclass
class ClassificationResult:
    """Output of the classify pipeline."""
    person_id: str | None  # "Jason", "Katie", or None
    confidence: float  # 0.0–1.0
    scores: dict[str, float] = field(default_factory=dict)  # raw scores per person


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIDENCE_RATIO_THRESHOLD = 0.65  # max(score) / sum(scores) >= 0.65
CONFIDENCE_ABS_THRESHOLD = 0.10   # max(score) > 0.1
TRAIL_EVENT_COUNT = 12            # default number of recent motion events

# Time-of-day bucket boundaries (EDT/EST)
TOD_BUCKETS = [
    ("morning", 6, 12),
    ("afternoon", 12, 18),
    ("evening", 18, 24),
    ("night", 0, 6),
]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
#
# Phase 5 (#399): the classifier moved into the alice repo but the profile
# files stayed in the vault. ``cozylobe_cortex_root()`` resolves the
# vault-resident cozylobe-cortex directory; everything else (people dir,
# guesses dir) is derived from it. ``COZYLOBE_CORTEX_PATH`` env var
# overrides the default so tests + alternate deployments can point the
# classifier at a fixture directory.

DEFAULT_COZYLOBE_CORTEX_PATH = Path.home() / "alice-mind" / "cozylobe-cortex"
# cortex-memory/people/ is the secondary source for non-behavioral facts
# (legacy from when profiles lived in the wider vault). Still consulted to
# augment behavioral data when both files exist for the same person.
DEFAULT_CORTEX_MEMORY_PEOPLE = Path.home() / "alice-mind" / "cortex-memory" / "people"


def cozylobe_cortex_root() -> Path:
    """Return the cozylobe-cortex vault directory.

    Honors ``COZYLOBE_CORTEX_PATH`` when set; otherwise defaults to
    ``~/alice-mind/cozylobe-cortex/`` (the bind-mounted vault inside the
    alice container).
    """
    env = os.environ.get("COZYLOBE_CORTEX_PATH")
    if env:
        return Path(env).expanduser()
    return DEFAULT_COZYLOBE_CORTEX_PATH


def _people_dir_coc() -> Path:
    return cozylobe_cortex_root() / "people"


def _guess_dir() -> Path:
    return cozylobe_cortex_root() / "guesses"


def _people_dir_cm() -> Path:
    """Fallback people directory in cortex-memory."""
    return DEFAULT_CORTEX_MEMORY_PEOPLE


# ---------------------------------------------------------------------------
# Trail builder: read last N motion events from guesses/
# ---------------------------------------------------------------------------

def build_trail(n: int = TRAIL_EVENT_COUNT) -> MotionTrail:
    """Build a motion trail from the most recent guess files.

    Reads the last N motion evidence entries from guess files in
    ``cozylobe-cortex/guesses/``, sorted by timestamp.

    .. warning::

       This entry-point exists for the standalone classify-latest CLI
       (``python -m cozylobe_cortex.classify``). The motion pipeline
       calls :func:`classify_from_trail` instead, passing the in-memory
       trail directly — the guess-file round-trip would be circular
       inside the pipeline (the pipeline writes those guess files).
    """
    events: list[MotionEvent] = []
    guess_dir = _guess_dir()
    if not guess_dir.exists():
        return MotionTrail()

    guess_files = sorted(
        [f for f in guess_dir.glob("*.md") if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    for gf in guess_files[:50]:  # scan last 50 files
        lines = gf.read_text().splitlines()
        in_evidence = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("evidence:"):
                in_evidence = True
                continue
            if in_evidence:
                if stripped.startswith("- kind: motion"):
                    entity = ""
                    ts_str = ""
                    room = "unknown"
                    # Parse the evidence entry
                    if "entity_id:" in stripped:
                        m = re.search(r"entity_id:\s*(\S+)", stripped)
                        if m:
                            entity = m.group(1)
                    if "ts:" in stripped:
                        m = re.search(r"ts:\s*(\S+)", stripped)
                        if m:
                            ts_str = m.group(1)
                    # Extract room from filename
                    fname = gf.stem  # e.g. "2026-05-26T215310Z-unknown-kitchen"
                    parts = fname.split("-")
                    if len(parts) >= 2:
                        room = parts[-1].replace("_", " ").title()
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        events.append(MotionEvent(
                            entity_id=entity,
                            room=room,
                            ts=ts,
                            state="on",
                        ))
                elif not stripped.startswith("  ") and not stripped.startswith("- ") and stripped and not stripped.startswith("#"):
                    in_evidence = False
            if len(events) >= n:
                break

    trail = MotionTrail(events=events[:n])
    return trail


# ---------------------------------------------------------------------------
# Profile loader: read behavioral data from people notes
# ---------------------------------------------------------------------------

def _parse_yaml_frontmatter(text: str) -> dict[str, Any]:
    """Parse the YAML frontmatter block of a markdown note.

    Callers in this module pass the full note text, so the leading
    ``---`` fenced block is located here. The body between the fences
    is delegated to :func:`yaml.safe_load` so nested mappings (e.g.
    ``cozylobe_behavioral: room_preferences: kitchen: 0.25``) are
    preserved. The previous regex-based parser only matched flat
    ``key: value`` lines and silently dropped every nested key, which
    left every behavioral profile empty and broke person attribution.

    Returns ``{}`` for empty input, missing/unclosed fences, invalid
    YAML, or a non-mapping document.
    """
    if not text or not text.strip():
        return {}

    # Extract the body between the leading ``---`` fences if present;
    # otherwise treat the whole string as a YAML document (back-compat
    # with any caller that pre-strips the fences).
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            # opening fence with no closing fence -> no frontmatter
            return {}
        body = "\n".join(lines[1:end])

    if not body.strip():
        return {}

    try:
        result = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        log.warning("Failed to parse YAML frontmatter: %s", exc)
        return {}

    return result if isinstance(result, dict) else {}


def _load_people_dir(directory: Path) -> dict[str, BehavioralProfile]:
    """Load behavioral profiles from a people directory."""
    profiles: dict[str, BehavioralProfile] = {}
    if not directory.exists():
        return profiles

    for note_file in directory.glob("*.md"):
        text = note_file.read_text()
        fm = _parse_yaml_frontmatter(text)
        name = note_file.stem  # e.g. "Jason"

        # Extract behavioral data
        room_prefs = {}
        tod = {}
        transitions = []
        session_len = 1800.0
        session_std = 900.0
        last_updated = None

        # Check for cozylobe_behavioral section
        cb = fm.get("cozylobe_behavioral", {})
        if isinstance(cb, dict):
            room_prefs = cb.get("room_preferences", {})
            tod = cb.get("time_of_day", {})
            transitions_raw = cb.get("common_transitions", [])
            if isinstance(transitions_raw, list):
                transitions = [tuple(t) for t in transitions_raw if isinstance(t, (list, tuple)) and len(t) == 2]
            session_len = float(cb.get("typical_session_length", 1800))
            last_updated_str = cb.get("last_updated")
            if last_updated_str:
                try:
                    last_updated = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

        # Fall back: extract what we can from the flat frontmatter
        if not room_prefs:
            # Check for room_preferences in flat fm
            rp = fm.get("room_preferences", {})
            if isinstance(rp, dict):
                room_prefs = rp
            elif isinstance(rp, list):
                for item in rp:
                    if isinstance(item, dict) and "room" in item and "weight" in item:
                        room_prefs[item["room"]] = float(item["weight"])

        if not tod:
            tod_raw = fm.get("time_of_day", {})
            if isinstance(tod_raw, dict):
                tod = tod_raw

        profile = BehavioralProfile(
            name=name,
            room_preferences=room_prefs,
            time_of_day=tod,
            common_transitions=transitions,
            typical_session_length=session_len,
            session_std=session_std,
            last_updated=last_updated,
        )
        profiles[name] = profile

    return profiles


def load_profiles() -> dict[str, BehavioralProfile]:
    """Load all known people profiles from both cozylobe-cortex and cortex-memory."""
    all_profiles: dict[str, BehavioralProfile] = {}

    # cozylobe-cortex/people/ takes precedence (more specific)
    coc_profiles = _load_people_dir(_people_dir_coc())
    for name, profile in coc_profiles.items():
        all_profiles[name] = profile

    # cortex-memory/people/ adds behavioral data
    cm_profiles = _load_people_dir(_people_dir_cm())
    for name, profile in cm_profiles.items():
        if name in all_profiles:
            # Merge: cortex-memory behavioral data augments cozylobe-cortex
            existing = all_profiles[name]
            if profile.room_preferences:
                existing.room_preferences.update(profile.room_preferences)
            if profile.time_of_day:
                existing.time_of_day.update(profile.time_of_day)
            if profile.common_transitions:
                existing.common_transitions.extend(profile.common_transitions)
            if profile.last_updated:
                existing.last_updated = profile.last_updated
        else:
            all_profiles[name] = profile

    return all_profiles


# ---------------------------------------------------------------------------
# Classification algorithm
# ---------------------------------------------------------------------------

def _dirichlet_multinomial_room_score(
    trail_rooms: set[str],
    room_durations: dict[str, float],
    room_prefs: dict[str, float],
) -> float:
    """Dirichlet-Multinomial likelihood of observed room visits given preferences.

    Uses room visit counts (number of distinct rooms visited) rather than
    raw counts or durations. Returns a probability density value.
    """
    if not room_prefs:
        return 0.0  # no behavioral data — always loses to any data-driven profile

    # Count distinct rooms visited in trail
    trail_room_set = trail_rooms
    if not trail_room_set:
        return 1.0

    # Dirichlet-Multinomial: prod(p_i ^ n_i) where n_i is visit count per room
    # Use counts (1 per room visited), not durations
    score = 1.0
    for room in trail_room_set:
        pref = room_prefs.get(room, 0.0)
        if pref > 0:
            score *= pref ** 1.0  # each visited room contributes one visit
        else:
            # Visited a room not in profile — penalize
            score *= 0.01  # small penalty for unlisted room

    # Bonus for rooms in profile that weren't visited (handles zero-visits gracefully)
    for room, pref in room_prefs.items():
        if room not in trail_room_set and pref > 0:
            score *= pref ** 0.5  # partial credit for profile match

    return score


def _time_of_day_score(
    first_seen: datetime | None,
    tod_prefs: dict[str, float],
) -> float:
    """Score based on time-of-day preference match."""
    if not tod_prefs or not first_seen:
        return 1.0  # uniform prior

    # Determine which bucket the first_seen falls into
    hour = first_seen.hour
    for bucket_name, start, end in TOD_BUCKETS:
        if start < end:
            if start <= hour < end:
                break
        else:  # wraps midnight (e.g., night: 0-6)
            if hour >= start or hour < end:
                break
    else:
        return 1.0

    pref = tod_prefs.get(bucket_name, 0.0)
    return pref if pref > 0 else 1.0


def _transition_score(
    trail_transitions: list[tuple[str, str]],
    common_transitions: list[tuple[str, str]],
) -> float:
    """Score based on transition frequency match."""
    if not trail_transitions or not common_transitions:
        return 1.0  # uniform prior, no discrimination

    matches = sum(
        1 for t in trail_transitions
        if t in common_transitions or (t[1], t[0]) in common_transitions  # bidirectional
    )
    return matches / len(trail_transitions)


def _session_length_score(
    session_len: float,
    typical_len: float,
    std: float,
) -> float:
    """Gaussian score for session length match."""
    if session_len <= 0 or typical_len <= 0:
        return 1.0
    if std <= 0:
        std = 900.0  # default fallback
    z = (session_len - typical_len) / std
    return math.exp(-0.5 * z * z)


def classify(trail: MotionTrail, profiles: dict[str, BehavioralProfile]) -> ClassificationResult:
    """Classify a motion trail against known people profiles.

    Returns (person_id, confidence, raw_scores).
    """
    if not trail.events:
        return ClassificationResult(person_id=None, confidence=0.0)

    if len(trail.events) < 2:
        return ClassificationResult(person_id=None, confidence=0.0)

    if not profiles:
        return ClassificationResult(person_id=None, confidence=0.0)

    # Check if any profile has actual behavioral data
    has_data = any(
        p.room_preferences or p.time_of_day or p.common_transitions
        for p in profiles.values()
    )
    if not has_data:
        return ClassificationResult(person_id=None, confidence=0.0)

    room_durations = trail.room_duration
    trail_transitions = trail.transitions
    session_len = trail.session_length

    # Compute scores for each person
    scores: dict[str, float] = {}
    for name, profile in profiles.items():
        room_score = _dirichlet_multinomial_room_score(
            trail.rooms_visited, room_durations, profile.room_preferences
        )
        tod_score = _time_of_day_score(trail.first_seen, profile.time_of_day)
        trans_score = _transition_score(trail_transitions, profile.common_transitions)
        session_score = _session_length_score(session_len, profile.typical_session_length, profile.session_std)

        # Composite score (product of factors)
        composite = room_score * tod_score * trans_score * session_score
        scores[name] = composite

    if not scores:
        return ClassificationResult(person_id=None, confidence=0.0)

    # Normalize scores to probability mass
    total = sum(scores.values())
    if total <= 0:
        return ClassificationResult(person_id=None, confidence=0.0)

    normalized = {name: s / total for name, s in scores.items()}

    # Check for uniform scores (all profiles equally likely → no discrimination)
    score_values = list(normalized.values())
    if len(score_values) > 1:
        max_val = max(score_values)
        min_val = min(score_values)
        if max_val - min_val < 1e-10:
            # All scores are effectively identical — no person stands out
            return ClassificationResult(person_id=None, confidence=0.0, scores=scores)

    # Find the best candidate
    best_person = max(normalized, key=normalized.get)
    best_confidence = normalized[best_person]

    # Apply confidence thresholds
    if best_confidence >= CONFIDENCE_RATIO_THRESHOLD and best_confidence > CONFIDENCE_ABS_THRESHOLD:
        person_id = best_person
    else:
        person_id = None

    return ClassificationResult(
        person_id=person_id,
        confidence=best_confidence if person_id else 0.0,
        scores=scores,
    )


# ---------------------------------------------------------------------------
# Convenience: one-shot classify
# ---------------------------------------------------------------------------

def classify_latest() -> ClassificationResult:
    """Classify the current motion trail against all known profiles."""
    trail = build_trail()
    profiles = load_profiles()
    return classify(trail, profiles)


# ---------------------------------------------------------------------------
# Phase 5 (#399): in-pipeline entry point
# ---------------------------------------------------------------------------

def classify_from_trail(
    trail_events: list["MotionEvent"],
    profiles: dict[str, BehavioralProfile] | None = None,
    n: int = TRAIL_EVENT_COUNT,
) -> ClassificationResult:
    """Classify from raw motion events (no guess-file round-trip).

    The motion pipeline holds the live trail in memory and can pass it
    here directly; :func:`build_trail` is for the standalone CLI path
    that reconstructs the trail from on-disk guess files.

    The caller's ``MotionEvent`` shape is the
    :mod:`cozylobe_cortex.classify` dataclass — the motion pipeline
    converts its own ``alice_cozylobe.motion.MotionEvent`` records into
    this shape at the call site (different field names: ``timestamp`` vs
    ``ts``, ``room_id`` vs ``room``).

    ``profiles=None`` loads the live vault. Tests pass an explicit dict
    to keep them hermetic.
    """
    events = list(trail_events[:n])  # take latest N
    trail = MotionTrail(events=events)
    if profiles is None:
        profiles = load_profiles()
    return classify(trail, profiles)


# ---------------------------------------------------------------------------
# Test / demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = classify_latest()
    print(f"Person: {result.person_id}")
    print(f"Confidence: {result.confidence:.4f}")
    if result.scores:
        print("Raw scores:")
        for name, score in sorted(result.scores.items(), key=lambda x: -x[1]):
            print(f"  {name}: {score:.6f}")
