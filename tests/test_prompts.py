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


# ---------------------------------------------------------------------------
# Phase 5 — per-event turn templates


# Every event kind that flows through the dispatcher needs a
# matching ``turn.<kind>.md.j2``. This test is the recurrence
# guard: catches "added a new transport but forgot the template"
# at CI time, not first live event.
TURN_KINDS = ("signal", "cli", "discord", "a2a", "surface", "emergency")


@pytest.mark.parametrize("kind", TURN_KINDS)
def test_every_event_kind_has_turn_template(kind: str):
    path = DEFAULTS_DIR / "speaking" / f"turn.{kind}.md.j2"
    assert path.is_file(), f"missing turn template: {path}"


def test_turn_cli_renders_with_context():
    rendered = load(
        "speaking.turn.cli",
        principal_name="Owner",
        stamp="now",
        text="hi",
        capability="(caps)",
    )
    assert "[CLI from Owner | now]" in rendered
    assert "hi" in rendered
    assert "(caps)" in rendered


def test_turn_signal_single_message_renders():
    """Single-envelope batches use the simple layout."""
    rendered = load(
        "speaking.turn.signal",
        sender_name="Owner",
        stamp="now",
        messages=[{"body": "hi", "attachments": [], "timestamp_str": "10:00"}],
        capability="(caps)",
    )
    assert "[Signal from Owner | now]" in rendered
    assert "hi" in rendered
    # Single-message branch does NOT include the "messages came in" preamble.
    assert "messages came in" not in rendered


def test_turn_signal_multi_message_renders():
    """Multi-envelope batches enumerate with timestamps."""
    rendered = load(
        "speaking.turn.signal",
        sender_name="Owner",
        stamp="now",
        messages=[
            {"body": "first", "attachments": [], "timestamp_str": "10:00"},
            {"body": "second", "attachments": [], "timestamp_str": "10:01"},
        ],
        capability="(caps)",
    )
    assert "2 messages came in" in rendered
    assert "first" in rendered
    assert "second" in rendered
    assert "10:01" in rendered


def test_turn_surface_renders_with_persona_default():
    rendered = load(
        "speaking.turn.surface",
        surface_id="2026-04-30T15-00.md",
        body="What if Owner needs lunch?",
    )
    assert "2026-04-30T15-00.md" in rendered
    # Placeholder persona default substituted ("the operator" — Plan 05
    # replaces with real personae).
    assert "the operator" in rendered


def test_turn_emergency_renders():
    rendered = load(
        "speaking.turn.emergency",
        emergency_id="hb-stale.md",
        body="heartbeat 50 minutes stale",
    )
    assert "hb-stale.md" in rendered
    assert "heartbeat 50 minutes stale" in rendered
    assert "EMERGENCY" in rendered


# ---------------------------------------------------------------------------
# Phase 6 — wake.active bootstrap template


def test_wake_active_template_renders_with_directive():
    """The wake.active template injects the timestamp header at the
    top, the directive (when supplied) under a Standing-orders
    heading, then the bootstrap body. Rendered output should contain
    all three sections."""
    rendered = load(
        "thinking.wake.active",
        timestamp_header="Current local time: 2026-04-30 12:00 EDT (Wednesday)",
        directive="Avoid drama. Ship the work.",
    )
    assert "Current local time: 2026-04-30" in rendered
    assert "Avoid drama. Ship the work." in rendered
    assert "Directive (current standing orders" in rendered
    # Bootstrap body still present (anchor near the start).
    assert "You are Alice in reflection" in rendered


def test_wake_active_template_skips_directive_when_empty():
    """Empty directive means no Standing-orders heading — the
    template's ``{% if directive %}`` guards it."""
    rendered = load(
        "thinking.wake.active",
        timestamp_header="Current local time: 2026-04-30 12:00 EDT",
        directive="",
    )
    assert "Current local time" in rendered
    assert "Directive (current standing orders" not in rendered
    assert "You are Alice in reflection" in rendered


# ---------------------------------------------------------------------------
# Phase 7 — daemon-built loader picks up mind override


def test_daemon_loader_uses_mind_override(tmp_path):
    """End-to-end: build the daemon-side loader against a fixture
    mind directory containing a custom template; the loader returns
    the override, not the runtime default."""
    from alice_speaking.factory import build_prompt_loader
    from alice_speaking.infra.config import Config, SPEAKING_DEFAULTS

    # Write a custom override at .alice/prompts/speaking/compact.md.j2.
    mind_dir = tmp_path / "alice-mind"
    override_dir = mind_dir / ".alice" / "prompts" / "speaking"
    override_dir.mkdir(parents=True)
    (override_dir / "compact.md.j2").write_text(
        "OVERRIDE compact for {{ user.name }}"
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg = Config(
        signal_api="x",
        signal_account="x",
        oauth_token="x",
        work_dir=mind_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=state_dir / "s.log",
        offset_path=state_dir / "off",
        seen_path=state_dir / "seen",
        turn_log_path=mind_dir / "turn.jsonl",
        event_log_path=state_dir / "events.log",
        speaking=dict(SPEAKING_DEFAULTS),
    )

    loader = build_prompt_loader(cfg)
    rendered = loader.load("speaking.compact")
    # Override wins over runtime default.
    assert rendered.startswith("OVERRIDE compact for")
    # Persona placeholder defaults still substitute.
    assert "the operator" in rendered


def test_mind_scaffold_includes_alice_prompts_dir():
    """Recurrence guard: the mind-scaffold ships ``.alice/prompts/``
    so freshly-scaffolded minds have the override path even when
    they don't customise anything yet."""
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    scaffold_prompts = repo_root / "templates" / "mind-scaffold" / ".alice" / "prompts"
    assert scaffold_prompts.is_dir(), (
        f"mind scaffold missing .alice/prompts/ at {scaffold_prompts}"
    )
    # Tracked-in-git via .gitkeep (otherwise alice-init's cp -a would
    # skip the empty directory).
    assert (scaffold_prompts / ".gitkeep").is_file()
