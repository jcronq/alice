"""Status-site source: slash-commands tab (issue #624)."""

from __future__ import annotations

from status_site.sources.slash_commands import (
    render_slash_commands_md,
    build_envelopes,
    render,
)


def test_render_slash_commands_md_lists_full_registry():
    body = render_slash_commands_md()
    # Overview copy
    assert "signal-ingest" in body.lower()
    assert "//foo" in body
    # Every verb appears
    for verb in (
        "interrupt", "cancel", "pause", "resume", "status",
        "quiet", "ack", "q",
        "remember", "forget", "recall",
        "go", "kill", "retry", "dispatch",
        "food", "workout", "weight", "log",
        "help",
    ):
        assert f"`/{verb}`" in body


def test_render_slash_commands_md_groups_by_category():
    body = render_slash_commands_md()
    for heading in ("Control", "Voice", "Memory", "Dispatch", "Skill", "Meta"):
        assert f"## {heading}" in body


def test_render_slash_commands_md_shows_acl():
    body = render_slash_commands_md()
    # Katie only appears next to her three allowed verbs — look for the
    # presence of "katie" in the rendered body at all.
    assert "katie" in body.lower()


def test_build_envelopes_returns_a2ui_shape():
    envelopes = build_envelopes()
    # Two envelopes: createSurface + updateComponents
    assert len(envelopes) >= 2
    assert any(
        isinstance(e, dict) and e.get("type") == "createSurface" for e in envelopes
    )


def test_render_returns_a2ui_payload():
    payload = render()
    assert "a2ui" in payload
    assert isinstance(payload["a2ui"], list)
