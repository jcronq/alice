"""Phase 1 tests for cozylobe-cortex (issue #378).

Covers the seven acceptance pieces from the prompt:

* Load empty vault returns empty Vault.
* Load populated vault returns expected entities.
* parse_inline_edges on prose with mixed bare + typed-weighted wikilinks.
* sensor → room lookup.
* Lint on valid + invalid vault.
* Onboarding fixture: simulated answers produce expected files.
* Onboarding rerun without --force refuses to overwrite.

The onboarding + lint implementations live in
``alice_cozylobe.cortex_cli`` (the ``scripts/cozylobe_cortex_*.py`` are
thin shims). Tests import the module directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alice_cozylobe import cortex_cli as _cortex_cli


@pytest.fixture
def onboard():
    return _cortex_cli


@pytest.fixture
def lint():
    return _cortex_cli


# ---------------------------------------------------------------------------
# Library: load_vault, parse_inline_edges, sensor_room

def test_load_empty_vault_returns_empty(tmp_path):
    from alice_cozylobe.cortex import load_vault

    vault = load_vault(tmp_path)
    assert vault.rooms == {}
    assert vault.sensors == {}
    assert vault.people == {}
    assert vault.all_notes() == []


def test_load_missing_root_returns_empty(tmp_path):
    from alice_cozylobe.cortex import load_vault

    vault = load_vault(tmp_path / "does-not-exist")
    assert vault.rooms == {}
    assert vault.all_notes() == []


def test_load_populated_vault(tmp_path, onboard):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen", "Living Room"]},
        adjacency={"Kitchen": ["Living Room"], "Living Room": ["Kitchen"]},
        sensor_to_room={
            "binary_sensor.hue_kitchen_motion": "Kitchen",
            "binary_sensor.hue_living_room_motion": "Living Room",
        },
        extra_people=[],
    )
    onboard.materialize(tmp_path, answers)

    from alice_cozylobe.cortex import load_vault

    vault = load_vault(tmp_path)
    assert {n.title for n in vault.rooms.values()} == {"Kitchen", "Living Room"}
    # Sensor titles are the prefix-stripped form (live vault convention).
    # The full HA entity_id is preserved in frontmatter `entity_id:`.
    assert {n.title for n in vault.sensors.values()} == {
        "hue_kitchen_motion",
        "hue_living_room_motion",
    }
    # Jason, Katie, Mike, unknown — the canonical four.
    assert {n.title for n in vault.people.values()} == {
        "Jason",
        "Katie",
        "Mike",
        "unknown",
    }


def test_parse_inline_edges_mixed_syntax():
    from alice_cozylobe.cortex import (
        DEFAULT_VERB,
        DEFAULT_WEIGHT,
        parse_inline_edges,
    )

    body = (
        "The Kitchen (IS-ADJACENT-TO:0.9)[[rooms/Living Room]] and a bare\n"
        "[[rooms/Office]] mention. A (DEPENDS-ON)[[trajectories/Morning]]\n"
        "edge with no explicit weight defaults to 1.0.\n"
    )
    edges = parse_inline_edges(body)
    assert len(edges) == 3

    e_adj = edges[0]
    assert e_adj.verb == "IS-ADJACENT-TO"
    assert e_adj.weight == pytest.approx(0.9)
    assert e_adj.target == "rooms/Living Room"
    assert e_adj.source_line == 1

    e_bare = edges[1]
    assert e_bare.verb == DEFAULT_VERB
    assert e_bare.weight == DEFAULT_WEIGHT
    assert e_bare.target == "rooms/Office"
    assert e_bare.source_line == 2

    e_dep = edges[2]
    assert e_dep.verb == "DEPENDS-ON"
    assert e_dep.weight == DEFAULT_WEIGHT
    assert e_dep.target == "trajectories/Morning"


def test_sensor_room_lookup(tmp_path, onboard):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen"]},
        adjacency={},
        sensor_to_room={"binary_sensor.hue_kitchen_motion": "Kitchen"},
    )
    onboard.materialize(tmp_path, answers)

    from alice_cozylobe.cortex import load_vault, sensor_room

    vault = load_vault(tmp_path)
    # Raw HA entity_id (with domain prefix) — prefix gets stripped before lookup.
    room = sensor_room(vault, "binary_sensor.hue_kitchen_motion")
    assert room is not None
    assert room.title == "Kitchen"

    # Bare title (already stripped) works too.
    room2 = sensor_room(vault, "hue_kitchen_motion")
    assert room2 is not None and room2.title == "Kitchen"

    # Full slug form (matches the live vault convention — no HA prefix).
    room3 = sensor_room(vault, "sensors/hue_kitchen_motion")
    assert room3 is not None and room3.title == "Kitchen"

    # Other HA domain prefixes should also strip cleanly.
    # (Not a sensor lookup we'd actually make, but verifies the prefix table.)
    for prefix in ("light.", "switch.", "fan.", "sensor."):
        # These would resolve to the same room if a sensor existed at that title.
        # Just ensure they don't crash.
        sensor_room(vault, f"{prefix}some_imaginary_thing")

    # Unknown sensor returns None.
    assert sensor_room(vault, "does-not-exist") is None
    assert sensor_room(vault, "binary_sensor.does_not_exist") is None


def test_vault_get_resolves_slug_and_title(tmp_path, onboard):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen"]},
        sensor_to_room={"binary_sensor.hue_kitchen_motion": "Kitchen"},
    )
    onboard.materialize(tmp_path, answers)

    from alice_cozylobe.cortex import load_vault

    vault = load_vault(tmp_path)
    note_by_slug = vault.get("rooms/Kitchen")
    note_by_title = vault.get("Kitchen", category="rooms")
    assert note_by_slug is not None and note_by_title is not None
    assert note_by_slug.slug == note_by_title.slug


# ---------------------------------------------------------------------------
# Lint

def test_lint_clean_vault_after_onboarding(tmp_path, onboard, lint):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen", "Living Room"]},
        adjacency={"Kitchen": ["Living Room"], "Living Room": ["Kitchen"]},
        sensor_to_room={
            "binary_sensor.hue_kitchen_motion": "Kitchen",
            "binary_sensor.hue_living_room_motion": "Living Room",
        },
    )
    onboard.materialize(tmp_path, answers)

    issues = lint.lint_vault(tmp_path)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], f"expected clean vault, got errors: {errors}"


def test_lint_flags_sensor_with_missing_room(tmp_path, onboard, lint):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen"]},
        sensor_to_room={"binary_sensor.hue_office_motion": "Office"},  # Office not in floors
    )
    onboard.materialize(tmp_path, answers)

    issues = lint.lint_vault(tmp_path)
    errors = [i for i in issues if i.severity == "error"]
    assert any("does not resolve to an existing room" in i.message for i in errors)


def test_lint_flags_orphan_note(tmp_path, onboard, lint):
    onboard.ensure_subdirs(tmp_path)
    # Drop a note outside the categorical subdirs.
    orphan = tmp_path / "stray.md"
    orphan.write_text("---\ntitle: stray\n---\n\nbody\n", encoding="utf-8")

    issues = lint.lint_vault(tmp_path)
    errors = [i for i in issues if i.severity == "error"]
    assert any("orphan note" in i.message for i in errors)


def test_lint_flags_malformed_edge_annotation(tmp_path, onboard, lint):
    onboard.ensure_subdirs(tmp_path)
    # Write a room note with a malformed annotation: lowercase verb +
    # garbage weight syntax.
    bad = tmp_path / "rooms" / "BadRoom.md"
    bad.write_text(
        "---\ntitle: BadRoom\ntags: [room, cozylobe-cortex]\n---\n\n"
        "Some prose with (lower-case:abc)[[rooms/Other]] which is malformed.\n",
        encoding="utf-8",
    )
    # Also drop the target so we don't conflate with the dangling-ref check.
    other = tmp_path / "rooms" / "Other.md"
    other.write_text(
        "---\ntitle: Other\ntags: [room, cozylobe-cortex]\n---\n\n# Other\n",
        encoding="utf-8",
    )

    issues = lint.lint_vault(tmp_path)
    errors = [i for i in issues if i.severity == "error"]
    assert any("malformed edge annotation" in i.message for i in errors)


def test_lint_warns_on_unknown_verb(tmp_path, onboard, lint):
    onboard.ensure_subdirs(tmp_path)
    # Valid syntax + unknown verb should warn but not error.
    note = tmp_path / "rooms" / "Kitchen.md"
    note.write_text(
        "---\ntitle: Kitchen\ntags: [room, cozylobe-cortex]\n---\n\n"
        "Kitchen (TELEPORTS-TO:1.0)[[rooms/Office]].\n",
        encoding="utf-8",
    )
    office = tmp_path / "rooms" / "Office.md"
    office.write_text(
        "---\ntitle: Office\ntags: [room, cozylobe-cortex]\n---\n\n# Office\n",
        encoding="utf-8",
    )

    issues = lint.lint_vault(tmp_path)
    warnings = [i for i in issues if i.severity == "warning"]
    errors = [i for i in issues if i.severity == "error"]
    assert any("unknown verb" in w.message for w in warnings)
    assert errors == [], f"unknown-verb should be warning, got errors: {errors}"


# ---------------------------------------------------------------------------
# Onboarding orchestration

def test_onboarding_writes_expected_files(tmp_path, onboard):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen", "Living Room"], "2": ["Master Bedroom"]},
        adjacency={
            "Kitchen": ["Living Room"],
            "Living Room": ["Kitchen"],
        },
        sensor_to_room={
            "binary_sensor.hue_kitchen_motion": "Kitchen",
            "binary_sensor.hue_master_motion": "Master Bedroom",
        },
        extra_people=[{"name": "Sarah", "role": "visitor"}],
    )
    written = onboard.materialize(tmp_path, answers)

    # Every floor's rooms got written.
    room_names = {p.stem for p in written["rooms"]}
    assert room_names == {"Kitchen", "Living Room", "Master Bedroom"}

    # Sensors — filename uses prefix-stripped form (live vault convention).
    sensor_names = {p.stem for p in written["sensors"]}
    assert "hue_kitchen_motion" in sensor_names
    assert "hue_master_motion" in sensor_names

    # People: canonical 4 + Sarah.
    people_names = {p.stem for p in written["people"]}
    assert {"Jason", "Katie", "Mike", "unknown", "Sarah"} <= people_names

    # Each subdir has its schema README copied in from templates/.
    for sub in ("rooms", "sensors", "people", "destinations",
                "trajectories", "guesses"):
        assert (tmp_path / sub / "README.md").exists(), \
            f"{sub}/README.md missing"
    # Top-level README too.
    assert (tmp_path / "README.md").exists()


def test_onboarding_refuses_to_overwrite_without_force(tmp_path, onboard):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen"]},
        sensor_to_room={"binary_sensor.hue_kitchen_motion": "Kitchen"},
    )
    onboard.materialize(tmp_path, answers)
    assert onboard.vault_has_notes(tmp_path) is True

    # Re-running through main() without --force should bail.
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(
        json.dumps({
            "floors": {"1": ["Kitchen"]},
            "sensor_to_room": {"binary_sensor.hue_kitchen_motion": "Kitchen"},
        }),
        encoding="utf-8",
    )
    rc = onboard.onboard_main(
        [
            "--vault", str(tmp_path),
            "--answers", str(answers_file),
            "--sensors-from", str(_empty_sensors_file(tmp_path)),
        ]
    )
    assert rc == 1


def test_onboarding_overwrites_with_force(tmp_path, onboard):
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen"]},
        sensor_to_room={"binary_sensor.hue_kitchen_motion": "Kitchen"},
    )
    onboard.materialize(tmp_path, answers)

    answers_file = tmp_path / "answers.json"
    answers_file.write_text(
        json.dumps({
            "floors": {"1": ["Kitchen", "Office"]},
            "sensor_to_room": {"binary_sensor.hue_kitchen_motion": "Kitchen"},
        }),
        encoding="utf-8",
    )
    rc = onboard.onboard_main(
        [
            "--vault", str(tmp_path),
            "--answers", str(answers_file),
            "--sensors-from", str(_empty_sensors_file(tmp_path)),
            "--force",
        ]
    )
    assert rc == 0
    assert (tmp_path / "rooms" / "Office.md").exists()


def test_onboarding_idempotent_no_diff(tmp_path, onboard):
    """Running materialize twice with the same answers should be a no-op
    (file contents unchanged)."""
    answers = onboard.OnboardingAnswers(
        floors={"1": ["Kitchen"]},
        sensor_to_room={"binary_sensor.hue_kitchen_motion": "Kitchen"},
    )
    onboard.materialize(tmp_path, answers)
    snapshots = {
        p: p.read_bytes()
        for p in tmp_path.rglob("*.md")
        if p.is_file()
    }
    onboard.materialize(tmp_path, answers)
    for p, before in snapshots.items():
        assert p.read_bytes() == before, f"changed: {p}"


# ---------------------------------------------------------------------------
# CozyHem entity-filtering helpers

def test_sensors_from_file_filters_to_motion(tmp_path, onboard):
    fixture = tmp_path / "entities.json"
    fixture.write_text(
        json.dumps([
            {"entity_id": "binary_sensor.hue_kitchen_motion"},
            {"entity_id": "binary_sensor.front_door_contact"},
            {"entity_id": "light.living_room"},
            {"entity_id": "binary_sensor.bedroom_motion_sensor"},
        ]),
        encoding="utf-8",
    )
    out = onboard.load_sensors_from_file(fixture)
    ids = {s["entity_id"] for s in out}
    assert ids == {
        "binary_sensor.hue_kitchen_motion",
        "binary_sensor.bedroom_motion_sensor",
    }


# ---------------------------------------------------------------------------
# Helpers

def _empty_sensors_file(tmp_path: Path) -> Path:
    p = tmp_path / "_empty-sensors.json"
    p.write_text("[]", encoding="utf-8")
    return p
