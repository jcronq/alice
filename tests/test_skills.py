"""Plan 07 Phases 1+4+6: skill registry tests.

Phase 1 — frontmatter parsing, ops nesting, error surfaces.
Phase 4 — description templating against personae.
Phase 6 — runtime defaults + override resolution from a mind dir.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from alice_skills import (
    DEFAULTS_DIR,
    Skill,
    SkillError,
    SkillRegistry,
)


def _write_skill(
    root: pathlib.Path,
    name: str,
    *,
    description: str = "Use when the user does X.",
    scope: str | None = None,
    body: str = "# title\n\nbody.\n",
    extra_frontmatter: dict | None = None,
    add_op: str | None = None,
) -> pathlib.Path:
    """Drop a skill at ``<root>/<name>/SKILL.md``. Returns the SKILL.md path."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {name}", f"description: {description}"]
    if scope is not None:
        fm_lines.append(f"scope: {scope}")
    for k, v in (extra_frontmatter or {}).items():
        fm_lines.append(f"{k}: {v}")
    skill_md = d / "SKILL.md"
    skill_md.write_text("---\n" + "\n".join(fm_lines) + "\n---\n\n" + body)
    if add_op is not None:
        ops_dir = d / "ops"
        ops_dir.mkdir(parents=True, exist_ok=True)
        (ops_dir / f"{add_op}.md").write_text(
            f"# {add_op} — sub-procedure\n\nWhen X, do Y.\n"
        )
    return skill_md


# ---------------------------------------------------------------------------
# Phase 1 — parsing


def test_parse_skill_with_minimal_frontmatter(tmp_path: pathlib.Path) -> None:
    skill_md = _write_skill(tmp_path, "log-meal", description="Log a meal.")
    s = Skill.parse(skill_md)
    assert s.name == "log-meal"
    assert s.description_template == "Log a meal."
    assert s.scope == "both"
    assert s.ops == ()
    assert "# title" in s.body


def test_parse_skill_reads_optional_scope(tmp_path: pathlib.Path) -> None:
    skill_md = _write_skill(tmp_path, "log-meal", scope="speaking")
    assert Skill.parse(skill_md).scope == "speaking"


def test_parse_skill_with_full_frontmatter(tmp_path: pathlib.Path) -> None:
    skill_md = _write_skill(
        tmp_path,
        "log-meal",
        description="Log a meal.",
        scope="speaking",
        extra_frontmatter={"fires_in_quiet_hours": "false", "emit_telemetry": "false"},
    )
    s = Skill.parse(skill_md)
    assert s.fires_in_quiet_hours is False
    assert s.emit_telemetry is False


def test_parse_skill_with_ops_subdirectory(tmp_path: pathlib.Path) -> None:
    """cortex-memory style — top-level SKILL.md plus ops/ files."""
    skill_md = _write_skill(
        tmp_path, "cortex-memory", scope="thinking", add_op="atomize"
    )
    s = Skill.parse(skill_md)
    assert s.scope == "thinking"
    assert len(s.ops) == 1
    op = s.ops[0]
    assert op.name == "atomize"
    # Ops inherit the parent's scope when absent on disk.
    assert op.scope == "thinking"
    # Default op description: first non-empty line of body.
    assert "atomize" in op.description_template


def test_parse_skill_raises_on_missing_description(tmp_path: pathlib.Path) -> None:
    d = tmp_path / "broken"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: broken\n---\n\nbody.\n")
    with pytest.raises(SkillError, match="description"):
        Skill.parse(d / "SKILL.md")


def test_parse_skill_raises_on_unknown_scope(tmp_path: pathlib.Path) -> None:
    skill_md = _write_skill(tmp_path, "weird", scope="viewer")
    with pytest.raises(SkillError, match="scope"):
        Skill.parse(skill_md)


def test_parse_skill_tolerates_unquoted_colons_in_description(
    tmp_path: pathlib.Path,
) -> None:
    """Real-world skill descriptions contain unquoted colons (e.g.
    ``"lunch: X"``). Strict YAML rejects these; the parser falls
    through to a forgiving line-based reader so user-authored
    SKILL.md files don't need to know about YAML escaping."""
    d = tmp_path / "log-meal"
    d.mkdir()
    (d / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: log-meal
            description: Use when user reports eating ("I ate X", "lunch: X").
            ---

            body.
            """
        )
    )
    s = Skill.parse(d / "SKILL.md")
    assert s.name == "log-meal"
    assert "lunch: X" in s.description_template


# ---------------------------------------------------------------------------
# Phase 1 — registry + override resolution


def test_registry_loads_every_skill_md(tmp_path: pathlib.Path) -> None:
    _write_skill(tmp_path, "a")
    _write_skill(tmp_path, "b")
    reg = SkillRegistry.from_search_paths([tmp_path])
    assert {s.name for s in reg.all()} == {"a", "b"}
    assert reg.find("a") is not None
    assert reg.find("nope") is None


def test_registry_resolves_override_from_mind_over_default(
    tmp_path: pathlib.Path,
) -> None:
    """A skill present in both paths → mind wins."""
    mind = tmp_path / "mind"
    runtime = tmp_path / "runtime"
    _write_skill(
        mind, "log-journal", description="Mind override for log-journal."
    )
    _write_skill(
        runtime, "log-journal", description="Runtime default for log-journal."
    )
    reg = SkillRegistry.from_search_paths([mind, runtime])
    assert reg.find("log-journal").description.startswith("Mind override")


def test_registry_for_hemisphere_filters_by_scope(tmp_path: pathlib.Path) -> None:
    _write_skill(tmp_path, "speak-only", scope="speaking")
    _write_skill(tmp_path, "think-only", scope="thinking")
    _write_skill(tmp_path, "shared", scope="both")
    reg = SkillRegistry.from_search_paths([tmp_path])
    speaking = {s.name for s in reg.for_hemisphere("speaking")}
    thinking = {s.name for s in reg.for_hemisphere("thinking")}
    assert speaking == {"speak-only", "shared"}
    assert thinking == {"think-only", "shared"}


def test_registry_for_hemisphere_both_returns_all(tmp_path: pathlib.Path) -> None:
    _write_skill(tmp_path, "a", scope="speaking")
    _write_skill(tmp_path, "b", scope="thinking")
    reg = SkillRegistry.from_search_paths([tmp_path])
    assert {s.name for s in reg.for_hemisphere("both")} == {"a", "b"}


def test_registry_is_skill_path_matches_skill_md(tmp_path: pathlib.Path) -> None:
    """Phase 5 telemetry uses ``is_skill_path`` to decide whether a
    Read counts as a skill load. Phase 1 ships the method; Phase 5
    wires the BlockHandler to call it."""
    skill_md = _write_skill(tmp_path, "x", add_op="atomize")
    reg = SkillRegistry.from_search_paths([tmp_path])
    assert reg.is_skill_path(skill_md) is True
    assert reg.is_skill_path(skill_md.parent / "ops" / "atomize.md") is True
    assert reg.is_skill_path(tmp_path / "unrelated.md") is False


# ---------------------------------------------------------------------------
# Phase 6 — runtime defaults


def test_defaults_dir_exists_and_contains_log_journal() -> None:
    """The runtime ships at least one default skill so other code
    paths (the inventory CLI, the scope-filter tests) have something
    to operate on without a mind dir."""
    assert DEFAULTS_DIR.is_dir()
    assert (DEFAULTS_DIR / "log-journal" / "SKILL.md").is_file()


def test_from_mind_includes_runtime_defaults_when_mind_absent(
    tmp_path: pathlib.Path,
) -> None:
    """A mind without its own ``.claude/skills/`` falls through to
    the runtime defaults."""
    reg = SkillRegistry.from_mind(tmp_path)
    names = {s.name for s in reg.all()}
    assert "log-journal" in names


def test_from_mind_override_wins_over_default(tmp_path: pathlib.Path) -> None:
    mind_skills = tmp_path / ".claude" / "skills"
    _write_skill(mind_skills, "log-journal", description="Mind wins.")
    reg = SkillRegistry.from_mind(tmp_path)
    assert reg.find("log-journal").description.startswith("Mind wins")


# ---------------------------------------------------------------------------
# Phase 4 — description templating


def test_describe_for_substitutes_user_name(tmp_path: pathlib.Path) -> None:
    from alice_core.config.personae import (
        AgentPersona,
        Personae,
        UserPersona,
    )

    skill_md = _write_skill(
        tmp_path,
        "log-meal",
        description="Use when {{ user.name }} reports eating something.",
    )
    s = Skill.parse(skill_md)
    p = Personae(agent=AgentPersona(name="Alice"), user=UserPersona(name="Jeremy"))
    rendered = s.describe_for(p)
    assert "Use when Jeremy reports eating something." == rendered
    # Raw template untouched — registry can re-render against a
    # different personae without re-parsing.
    assert "{{ user.name }}" in s.description_template


def test_describe_for_handles_agent_name(tmp_path: pathlib.Path) -> None:
    from alice_core.config.personae import (
        AgentPersona,
        Personae,
        UserPersona,
    )

    skill_md = _write_skill(
        tmp_path,
        "ask-self",
        description="Use when {{ agent.name }} needs to introspect.",
    )
    s = Skill.parse(skill_md)
    p = Personae(agent=AgentPersona(name="Eve"), user=UserPersona(name="Jordan"))
    assert s.describe_for(p) == "Use when Eve needs to introspect."


def test_describe_for_unchanged_when_no_template(tmp_path: pathlib.Path) -> None:
    """Skills without ``{{`` in the description don't trigger the
    Jinja render path — so a stray description with no placeholders
    is byte-identical post-render."""
    skill_md = _write_skill(
        tmp_path, "static-skill", description="Plain literal description."
    )
    s = Skill.parse(skill_md)
    assert s.describe_for(personae=None) == "Plain literal description."
