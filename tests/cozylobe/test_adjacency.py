"""Tests for AdjacencyInferrer (Phase 4 of #381).

Coverage:

* AdjacencyInferrer counts only non-adjacent pairs that fall inside
  the configured window; respects the vault's per-room adjacent list.
* Promotion curve: 5 obs → weight 0.3; subsequent observations bump
  weight along the curve and asymptote at 0.9.
* Promotion writes are atomic (no torn file) and update the right
  room note on both sides of the pair.
* weight_for_count's threshold table matches the design's curve.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alice_cozylobe.adjacency import (
    DEFAULT_INFERENCE_WINDOW_S,
    DEFAULT_PROMOTION_THRESHOLD,
    PROMOTION_CURVE,
    AdjacencyInferrer,
    weight_for_count,
)
from alice_cozylobe.cortex import load_vault
from alice_cozylobe.motion import MotionEvent


# ---------------------------------------------------------------------------
# Vault fixture


def _write_room(
    vault: Path, title: str, *, adjacent: list[str] | None = None
) -> None:
    rooms_dir = vault / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    adj_line = ""
    if adjacent:
        adj_line = "adjacent: " + ", ".join(f"[[rooms/{a}]]" for a in adjacent) + "\n"
    body = (
        "---\n"
        f"title: {title}\n"
        "tags: [room, cozylobe-cortex]\n"
        "created: 2026-05-26\n"
        "updated: 2026-05-26\n"
        + adj_line
        + "---\n\n"
        f"# {title}\n\n"
        f"Test room {title}.\n"
    )
    (rooms_dir / f"{title}.md").write_text(body, encoding="utf-8")


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """Three-room vault: Kitchen-Hallway-Bedroom in a line, Office
    isolated. The Kitchen↔Hallway and Hallway↔Bedroom edges are known
    (frontmatter); Kitchen↔Bedroom is NOT known and is the target
    of inference."""
    _write_room(tmp_path, "Kitchen", adjacent=["Hallway"])
    _write_room(tmp_path, "Hallway", adjacent=["Kitchen", "Bedroom"])
    _write_room(tmp_path, "Bedroom", adjacent=["Hallway"])
    _write_room(tmp_path, "Office")  # isolated, no adjacencies
    return tmp_path


def _motion(room: str, ts: float, entity: str | None = None) -> MotionEvent:
    return MotionEvent(
        timestamp=ts,
        entity_id=entity or f"hue_{room.lower()}_motion",
        state="on",
        room_id=room,
    )


# ---------------------------------------------------------------------------
# Pure helpers


def test_weight_for_count_below_threshold_returns_none():
    assert weight_for_count(0) is None
    assert weight_for_count(4) is None


def test_weight_for_count_matches_promotion_curve():
    # Spec: 5 obs → 0.3, 10 → 0.5, 20 → 0.7, 50 → 0.9 (asymptote).
    assert weight_for_count(5) == pytest.approx(0.3)
    assert weight_for_count(9) == pytest.approx(0.3)
    assert weight_for_count(10) == pytest.approx(0.5)
    assert weight_for_count(19) == pytest.approx(0.5)
    assert weight_for_count(20) == pytest.approx(0.7)
    assert weight_for_count(49) == pytest.approx(0.7)
    assert weight_for_count(50) == pytest.approx(0.9)
    # Asymptote: further observations don't push past 0.9.
    assert weight_for_count(1000) == pytest.approx(0.9)


def test_promotion_curve_is_sorted_ascending():
    counts = [c for c, _ in PROMOTION_CURVE]
    assert counts == sorted(counts)


# ---------------------------------------------------------------------------
# AdjacencyInferrer


def test_observe_ignores_known_adjacent_pairs(vault_path: Path):
    """Kitchen↔Hallway IS known-adjacent per the vault. Observing 100
    Kitchen→Hallway pairs must NOT increment any counter."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    trail = []
    ts = 1000.0
    for _ in range(100):
        trail.append(_motion("Kitchen", ts))
        ts += 5
        trail.append(_motion("Hallway", ts))
        ts += 5
    inferrer.observe(trail)
    # Kitchen-Hallway pair is known-adjacent → never counted.
    assert frozenset({"Kitchen", "Hallway"}) not in inferrer.counter


def test_observe_counts_non_adjacent_pairs(vault_path: Path):
    """Kitchen↔Bedroom is NOT known-adjacent. Three consecutive
    Kitchen→Bedroom→Kitchen transitions should bump the counter to 3
    — below the promotion threshold so no edge is written yet."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    # 4 events → 3 transitions: K→B, B→K, K→B (all non-adjacent).
    trail = [
        _motion("Kitchen", 1000.0),
        _motion("Bedroom", 1005.0),
        _motion("Kitchen", 1010.0),
        _motion("Bedroom", 1015.0),
    ]
    promoted = inferrer.observe(trail)
    assert promoted == []
    assert inferrer.counter[frozenset({"Kitchen", "Bedroom"})] == 3


def test_observe_skips_pairs_outside_window(vault_path: Path):
    """Two non-adjacent events > 30s apart should NOT count."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault, window_s=30.0)
    trail = [
        _motion("Kitchen", 1000.0),
        _motion("Bedroom", 1100.0),  # 100s later — too far
    ]
    inferrer.observe(trail)
    assert frozenset({"Kitchen", "Bedroom"}) not in inferrer.counter


def test_observe_skips_same_room_pairs(vault_path: Path):
    """Two consecutive Kitchen events shouldn't count as a transition."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    trail = [
        _motion("Kitchen", 1000.0),
        _motion("Kitchen", 1005.0),
    ]
    inferrer.observe(trail)
    assert not inferrer.counter


def test_observe_skips_events_with_no_room(vault_path: Path):
    """Events whose room_id is None (sensor not yet in vault) should
    not contribute to any pair."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    trail = [
        MotionEvent(
            timestamp=1000.0,
            entity_id="unknown_sensor",
            state="on",
            room_id=None,
        ),
        _motion("Bedroom", 1005.0),
    ]
    inferrer.observe(trail)
    assert not inferrer.counter


def test_promotion_at_threshold_writes_inline_edge(vault_path: Path):
    """5 observations of Kitchen↔Bedroom must write
    (IS-ADJACENT-TO:0.30)[[rooms/Bedroom]] into Kitchen's note AND
    (IS-ADJACENT-TO:0.30)[[rooms/Kitchen]] into Bedroom's note."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    ts = 1000.0
    trail = []
    for _ in range(5):
        trail.append(_motion("Kitchen", ts))
        ts += 5
        trail.append(_motion("Bedroom", ts))
        ts += 5
    promoted = inferrer.observe(trail)
    assert len(promoted) == 1
    a, b, weight = promoted[0]
    assert {a, b} == {"Kitchen", "Bedroom"}
    assert weight == pytest.approx(0.3)

    kitchen_text = (vault_path / "rooms" / "Kitchen.md").read_text()
    bedroom_text = (vault_path / "rooms" / "Bedroom.md").read_text()
    assert "(IS-ADJACENT-TO:0.30)[[rooms/Bedroom]]" in kitchen_text
    assert "(IS-ADJACENT-TO:0.30)[[rooms/Kitchen]]" in bedroom_text


def test_promotion_curve_updates_in_place_not_appended(vault_path: Path):
    """Crossing the 5 → 10 threshold must rewrite the weight on the
    existing line, not append a duplicate entry."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    ts = 1000.0
    trail = []
    # 10 observations — should promote at 5 (weight=0.3), then promote
    # again at 10 (weight=0.5).
    for _ in range(10):
        trail.append(_motion("Kitchen", ts))
        ts += 2
        trail.append(_motion("Bedroom", ts))
        ts += 2
    promoted = inferrer.observe(trail)
    # Two promotions on this single observe call (5 and 10).
    assert len(promoted) == 2
    assert promoted[-1][2] == pytest.approx(0.5)

    kitchen_text = (vault_path / "rooms" / "Kitchen.md").read_text()
    # Exactly ONE IS-ADJACENT-TO edge pointing at Bedroom.
    occurrences = kitchen_text.count("[[rooms/Bedroom]]")
    assert occurrences == 1, (
        "expected exactly one Bedroom edge in Kitchen.md, "
        f"got {occurrences}; body={kitchen_text!r}"
    )
    assert "(IS-ADJACENT-TO:0.50)[[rooms/Bedroom]]" in kitchen_text


def test_promotion_updates_frontmatter_updated_timestamp(vault_path: Path):
    """The room note's frontmatter ``updated:`` field is rewritten on
    promotion so vault grooming sees the change."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    original = (vault_path / "rooms" / "Kitchen.md").read_text()
    assert "updated: 2026-05-26" in original

    ts = 1000.0
    trail = []
    for _ in range(5):
        trail.append(_motion("Kitchen", ts))
        ts += 2
        trail.append(_motion("Bedroom", ts))
        ts += 2
    inferrer.observe(trail)

    new_text = (vault_path / "rooms" / "Kitchen.md").read_text()
    # The new timestamp uses the UTC ISO date.
    assert new_text.count("updated:") == 1
    assert "updated: 2026-05-26" not in new_text or "UTC" in new_text


def test_atomic_write_no_tmp_left_behind(vault_path: Path):
    """Promotion writes use tempfile + os.replace. After a successful
    promotion there must be no .tmp- siblings left in the rooms dir."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    ts = 1000.0
    trail = []
    for _ in range(5):
        trail.append(_motion("Kitchen", ts))
        ts += 2
        trail.append(_motion("Bedroom", ts))
        ts += 2
    inferrer.observe(trail)
    rooms_dir = vault_path / "rooms"
    tmp_siblings = list(rooms_dir.glob(".tmp-*"))
    assert tmp_siblings == [], f"leftover tempfiles: {tmp_siblings}"


def test_observe_handles_event_with_unknown_room(vault_path: Path):
    """A motion event whose room_id isn't in the vault (e.g. typo'd
    sensor mapping) must not crash the inferrer — the pair is silently
    skipped."""
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    trail = [
        _motion("Kitchen", 1000.0),
        MotionEvent(
            timestamp=1005.0,
            entity_id="hue_attic_motion",
            state="on",
            room_id="Attic",  # not in vault
        ),
    ]
    inferrer.observe(trail)
    assert frozenset({"Kitchen", "Attic"}) not in inferrer.counter


def test_promotion_threshold_constant_matches_curve():
    """DEFAULT_PROMOTION_THRESHOLD should equal the first curve entry."""
    first_count, _ = PROMOTION_CURVE[0]
    assert DEFAULT_PROMOTION_THRESHOLD == first_count


def test_default_window_constant_is_30_seconds():
    """Matches the design's ``adjacency_inference_window_s: 30.0`` default."""
    assert DEFAULT_INFERENCE_WINDOW_S == 30.0


def test_observe_skips_rewrites_below_next_threshold(vault_path: Path):
    """Within a single threshold band, repeated observations should NOT
    rewrite the room note — the promotion latch holds until the next
    threshold crosses.

    Build a trail that crosses the 5-threshold (weight 0.3) but stays
    below 10. Two separate observe() calls. First call writes at 0.3;
    second call sees the same weight target and skips the rewrite.
    """
    vault = load_vault(vault_path)
    inferrer = AdjacencyInferrer(vault=vault)
    # 6 events → 5 transitions, all K↔B. Hits threshold 5 (weight 0.3),
    # counter ends at 5 — below 10.
    trail_a = [
        _motion("Kitchen", 1000.0),
        _motion("Bedroom", 1002.0),
        _motion("Kitchen", 1004.0),
        _motion("Bedroom", 1006.0),
        _motion("Kitchen", 1008.0),
        _motion("Bedroom", 1010.0),
    ]
    promoted_1 = inferrer.observe(trail_a)
    assert len(promoted_1) == 1
    # A second observe of a 2-event trail (1 transition) bumps the
    # counter to 6 — still inside the 0.3 band. No re-promotion.
    trail_b = [_motion("Kitchen", 2000.0), _motion("Bedroom", 2002.0)]
    promoted_2 = inferrer.observe(trail_b)
    assert promoted_2 == []
