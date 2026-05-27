"""Unit tests for :func:`cozylobe_cortex.classify._parse_yaml_frontmatter`.

The parser was previously a hand-rolled regex that only matched flat
``key: value`` lines and silently dropped every nested key. That meant
every behavioral profile (``cozylobe_behavioral: room_preferences: ...``)
loaded with empty data, leaving the person-attribution pipeline returning
``unknown`` for 263+ guesses.

These tests pin the contract for the :func:`yaml.safe_load`-based
replacement: nested keys preserved, invalid YAML returns ``{}`` without
raising, and a real profile fixture round-trips with non-empty
behavioral data.
"""

from __future__ import annotations

from textwrap import dedent

from cozylobe_cortex.classify import _parse_yaml_frontmatter


def test_empty_input_returns_empty_dict() -> None:
    assert _parse_yaml_frontmatter("") == {}
    assert _parse_yaml_frontmatter("   \n\n  ") == {}


def test_flat_keys_preserved() -> None:
    """Flat ``key: value`` parity with the old regex parser."""
    text = dedent(
        """\
        ---
        title: Jason
        created: 2026-05-26
        access_count: 7
        ---

        # body
        """
    )
    fm = _parse_yaml_frontmatter(text)
    assert fm["title"] == "Jason"
    assert fm["created"].isoformat() == "2026-05-26"
    assert fm["access_count"] == 7


def test_nested_keys_preserved_regression() -> None:
    """Regression: nested mappings under ``cozylobe_behavioral`` survive.

    This is the core bug the parser fix addresses. The old regex
    parser flattened the structure and dropped every nested key.
    """
    text = dedent(
        """\
        ---
        title: Jason
        cozylobe_behavioral:
          room_preferences:
            kitchen: 0.35
            bedroom: 0.30
            gym: 0.15
          time_of_day:
            morning: 0.40
            evening: 0.25
          common_transitions:
            - [kitchen, living_room]
            - [bedroom, bathroom]
          typical_session_length: 1800
        ---
        """
    )
    fm = _parse_yaml_frontmatter(text)
    cb = fm["cozylobe_behavioral"]
    assert cb["room_preferences"] == {
        "kitchen": 0.35,
        "bedroom": 0.30,
        "gym": 0.15,
    }
    assert cb["time_of_day"] == {"morning": 0.40, "evening": 0.25}
    assert cb["common_transitions"] == [
        ["kitchen", "living_room"],
        ["bedroom", "bathroom"],
    ]
    assert cb["typical_session_length"] == 1800


def test_invalid_yaml_returns_empty_dict_no_raise(caplog) -> None:
    """Malformed YAML must not crash the profile loader."""
    text = dedent(
        """\
        ---
        title: Bad
        broken: [unclosed, list
        nested:
          key: value
            misindented
        ---
        """
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="cozylobe_cortex.classify"):
        result = _parse_yaml_frontmatter(text)
    assert result == {}
    assert any(
        "Failed to parse YAML frontmatter" in rec.message
        for rec in caplog.records
    )


def test_non_mapping_yaml_returns_empty_dict() -> None:
    """A bare scalar or list document is not a profile mapping."""
    # Bare string (no fences -> whole body is a string scalar)
    assert _parse_yaml_frontmatter("just a string") == {}
    # Bare list inside fences
    fenced_list = dedent(
        """\
        ---
        - item1
        - item2
        ---
        """
    )
    assert _parse_yaml_frontmatter(fenced_list) == {}


def test_missing_closing_fence_returns_empty_dict() -> None:
    """An opening fence with no closing fence has no frontmatter."""
    text = "---\ntitle: Orphaned\nbody continues forever\n"
    assert _parse_yaml_frontmatter(text) == {}


def test_real_profile_fixture_katie() -> None:
    """End-to-end: a real behavioral profile produces non-empty data.

    The fixture is sourced from the vault profile that classify.py
    actually loads at runtime — this is the smoke test that the fix
    unblocks person attribution for at least one real resident.
    """
    text = dedent(
        """\
        ---
        title: Katie
        tags: [person, resident, cozylobe-cortex]
        created: 2026-05-26
        updated: 2026-05-27 10:38 EDT
        cozylobe_behavioral:
          room_preferences:
            bedroom: 0.30
            kitchen: 0.20
            living_room: 0.20
            bathroom: 0.15
            other: 0.15
          time_of_day:
            morning: 0.25
            afternoon: 0.30
            evening: 0.30
            night: 0.15
          common_transitions: []
          typical_session_length: 1800
        ---

        # Katie

        Resident.
        """
    )
    fm = _parse_yaml_frontmatter(text)
    cb = fm["cozylobe_behavioral"]
    # The two keys that drove the bug: under the old parser, both
    # were empty.
    assert cb["room_preferences"], "room_preferences must be non-empty"
    assert cb["time_of_day"], "time_of_day must be non-empty"
    assert cb["room_preferences"]["bedroom"] == 0.30
    assert cb["time_of_day"]["afternoon"] == 0.30
