"""Phase 1 of plan 04: PromptLoader.

Three contracts:

1. The default loader finds the templates shipped with the package
   under ``src/alice_prompts/templates/``.
2. A render context (kwargs to :meth:`PromptLoader.load`) substitutes
   ``{{var}}`` placeholders correctly.
3. An override path (``mind/.alice/prompts/``) wins over the package
   defaults — that's the per-mind customisation hook for plan 07.
4. Missing names raise :class:`PromptNotFound` with a helpful
   message (no surprise ``TemplateNotFound`` from Jinja).
"""

from __future__ import annotations

import pathlib

import pytest

from alice_prompts import (
    DEFAULTS_DIR,
    PromptLoader,
    PromptNotFound,
    load,
)


# ---------------------------------------------------------------------------
# Default-loader path (the singleton inside ``alice_prompts.__init__``)


def test_loader_finds_default_template():
    """The shipped ``thinking/quick.md.j2`` resolves via the
    package-level :func:`load`."""
    rendered = load("thinking.quick")
    assert "QUICK-OK" in rendered


def test_default_loader_lists_quick_template():
    from alice_prompts import list_prompts
    assert "thinking.quick" in list_prompts()


# ---------------------------------------------------------------------------
# Custom loader against a tmp tree (so we can exercise context + override
# resolution without touching the package's actual templates).


def _write(path: pathlib.Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_loader_renders_with_context(tmp_path: pathlib.Path):
    defaults = tmp_path / "defaults"
    _write(
        defaults / "speaking" / "hello.md.j2",
        "Hello {{ name }}",
    )
    loader = PromptLoader(defaults_path=defaults)
    assert loader.load("speaking.hello", name="Owner") == "Hello Owner"


def test_loader_raises_when_template_missing(tmp_path: pathlib.Path):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    loader = PromptLoader(defaults_path=defaults)
    with pytest.raises(PromptNotFound, match="thinking.unknown"):
        loader.load("thinking.unknown")


def test_override_wins_over_default(tmp_path: pathlib.Path):
    """Per-mind override hook: a same-named file under the override
    path resolves before the runtime default."""
    defaults = tmp_path / "defaults"
    override = tmp_path / "override"
    _write(defaults / "speaking" / "compact.md.j2", "DEFAULT")
    _write(override / "speaking" / "compact.md.j2", "OVERRIDE")

    loader = PromptLoader(
        defaults_path=defaults, override_path=override
    )
    assert loader.load("speaking.compact") == "OVERRIDE"


def test_override_missing_falls_back_to_default(tmp_path: pathlib.Path):
    """Override path can be empty or non-existent; the default still
    resolves. Useful for fresh installs that haven't customised."""
    defaults = tmp_path / "defaults"
    override = tmp_path / "override-does-not-exist"
    _write(defaults / "speaking" / "compact.md.j2", "DEFAULT")

    loader = PromptLoader(
        defaults_path=defaults, override_path=override
    )
    assert loader.load("speaking.compact") == "DEFAULT"


def test_loader_raises_when_defaults_dir_missing(tmp_path: pathlib.Path):
    """The constructor fails fast if the defaults path is bogus —
    catches packaging mistakes (templates not bundled in the wheel)
    rather than failing on first ``load`` call deep in production."""
    with pytest.raises(FileNotFoundError, match="defaults"):
        PromptLoader(defaults_path=tmp_path / "nope")


# ---------------------------------------------------------------------------
# Listing


def test_list_prompts_returns_sorted_names(tmp_path: pathlib.Path):
    defaults = tmp_path / "defaults"
    _write(defaults / "speaking" / "compact.md.j2", "x")
    _write(defaults / "thinking" / "quick.md.j2", "y")
    _write(
        defaults / "speaking" / "capability.signal.md.j2", "z"
    )
    loader = PromptLoader(defaults_path=defaults)
    assert loader.list_prompts() == [
        "speaking.capability.signal",
        "speaking.compact",
        "thinking.quick",
    ]


def test_default_dir_constant_points_at_runtime_templates():
    """The package-level DEFAULTS_DIR resolves to the shipped templates
    directory (sibling of ``loader.py``). Pin this so a future
    refactor that moves the templates folder fails this test rather
    than mysteriously dropping every prompt."""
    assert DEFAULTS_DIR.is_dir()
    assert (DEFAULTS_DIR / "thinking" / "quick.md.j2").is_file()


# ---------------------------------------------------------------------------
# Phase 2 — compact + sanity templates


def test_compact_template_renders_with_persona_placeholders():
    """The compact template uses ``{{user.name}}``; the package-level
    loader's placeholder defaults make it render as ``the operator``
    until plan 05 wires real personae."""
    rendered = load("speaking.compact")
    # No literal Jinja tags should leak.
    assert "{{" not in rendered
    # The placeholder default substituted in.
    assert "the operator" in rendered
    # Structural anchors still present.
    assert "Active threads" in rendered
    assert "Uncaptured facts" in rendered


def test_sanity_template_renders():
    """The sanity smoke's system prompt comes from
    ``meta/sanity.md.j2``. Single-line prompt, no placeholders."""
    rendered = load("meta.sanity").strip()
    assert rendered == "Reply verbatim to anything the user says. No preamble."


# ---------------------------------------------------------------------------
# Phase 3 — capability templates


def test_capability_template_per_transport_exists():
    """Recurrence guard: every transport class declared in the
    speaking registry must have a matching capability template.
    Catches "added a new transport but forgot the template" at
    CI time, not on the first live event."""
    from alice_speaking.transports import CLITransport, SignalTransport
    from alice_speaking.transports.a2a import A2ATransport
    from alice_speaking.transports.discord import DiscordTransport

    for transport_cls in (
        SignalTransport,
        CLITransport,
        DiscordTransport,
        A2ATransport,
    ):
        # transport.name is the lowercase identifier the loader uses.
        path = (
            DEFAULTS_DIR
            / "speaking"
            / f"capability.{transport_cls.name}.md.j2"
        )
        assert path.is_file(), (
            f"capability template missing for transport "
            f"{transport_cls.name!r}: expected {path}"
        )


def test_capability_signal_template_renders_with_caps():
    """The capability templates take ``caps`` and substitute
    ``{{caps.max_message_bytes}}`` etc. Pin against the real
    Signal caps so rendering changes surface as test failures."""
    from alice_speaking.transports.base import SIGNAL_CAPS

    rendered = load("speaking.capability.signal", caps=SIGNAL_CAPS)
    assert "**signal** transport" in rendered
    assert str(SIGNAL_CAPS.max_message_bytes) in rendered
    # Signal renders zero markdown.
    assert "PLAIN TEXT only" in rendered


def test_capability_cli_template_marks_interactive():
    """CLI is the one transport whose capability fragment notes
    the user is waiting at a terminal."""
    from alice_speaking.transports.base import CLI_CAPS

    rendered = load("speaking.capability.cli", caps=CLI_CAPS)
    assert "interactive session" in rendered


# ---------------------------------------------------------------------------
# Phase 4 — viewer narrative templates


def test_narrative_templates_exist():
    for stem in ("narrative.window", "narrative.bucket", "narrative.weave"):
        path = DEFAULTS_DIR / "viewer" / f"{stem}.md.j2"
        assert path.is_file(), f"missing template: {path}"


def test_narrative_window_renders_with_digest():
    rendered = load(
        "viewer.narrative.window",
        digest_json='{"events": []}',
        window_label="6h",
    )
    assert "Alice" in rendered  # placeholder agent.name default
    assert '{"events": []}' in rendered
    assert "6h" in rendered


def test_narrative_bucket_renders_with_window():
    rendered = load(
        "viewer.narrative.bucket",
        start="2026-04-30 10:00",
        end="10:30",
        events="(no events)",
    )
    assert "10:00" in rendered
    assert "10:30" in rendered
    assert "(no events)" in rendered


def test_narrative_weave_renders_with_body():
    rendered = load(
        "viewer.narrative.weave",
        body="[10:00] (3 events) hi",
        window_label="day",
    )
    assert "[10:00]" in rendered
    assert "day" in rendered
