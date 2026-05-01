"""Tests for alice_skills.render.render_to_disk.

Pin the strict-YAML output (the killer bug from the pi spike), the
hemisphere filtering, the README.md exclusion, and the atomic
clear-then-write semantics.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from alice_skills.registry import SkillRegistry
from alice_skills.render import render_to_disk


def _write_skill(skills_dir: pathlib.Path, name: str, body: str) -> None:
    (skills_dir / name).mkdir(parents=True, exist_ok=True)
    (skills_dir / name / "SKILL.md").write_text(body)


def _personae():
    """Stand-in for alice_core.config.personae.Personae — only need
    the as_template_context() shape."""
    return SimpleNamespace(
        as_template_context=lambda: {
            "user": SimpleNamespace(name="Jason", role="operator"),
            "agent": SimpleNamespace(name="Alice"),
        }
    )


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """Build a small mind with three skills (one per scope) and a
    README.md to verify it gets ignored."""
    mind = tmp_path / "alice-mind"
    skills = mind / ".claude" / "skills"
    skills.mkdir(parents=True)
    (mind / "CLAUDE.md").write_text("# Alice\nMind-level instructions.\n")
    (skills / "README.md").write_text("Skills live one per directory.\n")

    _write_skill(
        skills,
        "log-meal",
        '---\n'
        'name: log-meal\n'
        # The colon-inside-quoted-string case that breaks strict YAML;
        # parsed leniently by alice_skills, must be re-emitted strict.
        'description: Use when {{ user.name }} reports eating ("lunch: X").\n'
        'scope: speaking\n'
        '---\n'
        '# log-meal\n\nProcedure body for {{ user.name }}.\n',
    )
    _write_skill(
        skills,
        "groom-vault",
        '---\n'
        'name: groom-vault\n'
        'description: Manage {{ agent.name }} vault.\n'
        'scope: thinking\n'
        '---\n'
        '# groom-vault\n\nThinking-side body.\n',
    )
    _write_skill(
        skills,
        "shared-thing",
        '---\n'
        'name: shared-thing\n'
        'description: Useful for both hemispheres.\n'
        'scope: both\n'
        '---\n'
        '# shared-thing\n\nNo Jinja here.\n',
    )
    return mind


def test_render_filters_by_hemisphere(mind: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Speaking renders log-meal + shared-thing; thinking renders
    groom-vault + shared-thing. The "both" scope appears in both."""
    registry = SkillRegistry.from_mind(mind, include_defaults=False)
    target = tmp_path / "state" / "alice-skills" / "speaking"

    rendered = render_to_disk(
        registry,
        hemisphere="speaking",
        target_dir=target,
        personae=_personae(),
        mind_dir=mind,
    )

    names = sorted(s.name for s in rendered)
    assert names == ["log-meal", "shared-thing"]
    assert (target / ".claude" / "skills" / "log-meal" / "SKILL.md").is_file()
    assert (target / ".claude" / "skills" / "shared-thing" / "SKILL.md").is_file()
    assert not (target / ".claude" / "skills" / "groom-vault").exists()


def test_render_emits_strict_yaml_frontmatter(
    mind: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The rendered SKILL.md MUST round-trip through strict YAML
    (yaml.safe_load) — that's the property pi's parser checks. The
    raw mind-side description has a "lunch: X" pattern that defeats
    strict YAML; the render layer is what fixes that."""
    import yaml

    registry = SkillRegistry.from_mind(mind, include_defaults=False)
    target = tmp_path / "state" / "speaking"
    render_to_disk(
        registry,
        hemisphere="speaking",
        target_dir=target,
        personae=_personae(),
    )

    rendered_text = (target / ".claude" / "skills" / "log-meal" / "SKILL.md").read_text()
    assert rendered_text.startswith("---\n")

    end = rendered_text.index("\n---\n", 4)
    yaml_block = rendered_text[4:end]
    parsed = yaml.safe_load(yaml_block)  # MUST NOT raise
    assert parsed["name"] == "log-meal"
    assert "Jason" in parsed["description"]
    assert "{{" not in parsed["description"]
    assert "lunch: X" in parsed["description"]


def test_render_substitutes_jinja_in_body(
    mind: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "state" / "speaking"
    registry = SkillRegistry.from_mind(mind, include_defaults=False)
    render_to_disk(
        registry, hemisphere="speaking", target_dir=target, personae=_personae()
    )
    body = (target / ".claude" / "skills" / "log-meal" / "SKILL.md").read_text()
    # The body's "{{ user.name }}" should now read "Jason".
    assert "Procedure body for Jason." in body
    assert "{{ user.name }}" not in body


def test_render_skips_root_readme(mind: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """README.md at the skills root must NOT be copied — pi treats it
    as a candidate skill and rejects it for missing frontmatter."""
    target = tmp_path / "state" / "speaking"
    registry = SkillRegistry.from_mind(mind, include_defaults=False)
    render_to_disk(
        registry, hemisphere="speaking", target_dir=target, personae=_personae()
    )
    rendered_skills = target / ".claude" / "skills"
    assert not (rendered_skills / "README.md").exists()
    # Only directories — no stray .md files.
    for child in rendered_skills.iterdir():
        assert child.is_dir(), f"unexpected non-dir at skills root: {child}"


def test_render_copies_claude_md_when_provided(
    mind: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "state" / "speaking"
    registry = SkillRegistry.from_mind(mind, include_defaults=False)
    render_to_disk(
        registry,
        hemisphere="speaking",
        target_dir=target,
        personae=_personae(),
        mind_dir=mind,
    )
    assert (target / "CLAUDE.md").is_file()
    assert "Mind-level instructions." in (target / "CLAUDE.md").read_text()


def test_render_clears_stale_target(
    mind: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A second render with different scoping must not leave behind
    skills from the first run."""
    target = tmp_path / "state" / "speaking"
    registry = SkillRegistry.from_mind(mind, include_defaults=False)

    # First render: filter by "both" — gets log-meal, groom-vault, shared-thing.
    render_to_disk(
        registry, hemisphere="both", target_dir=target, personae=_personae()
    )
    assert (target / ".claude" / "skills" / "groom-vault").exists()

    # Second render: speaking only — groom-vault must disappear.
    render_to_disk(
        registry, hemisphere="speaking", target_dir=target, personae=_personae()
    )
    assert not (target / ".claude" / "skills" / "groom-vault").exists()
    assert (target / ".claude" / "skills" / "log-meal").exists()


def test_render_without_personae_leaves_jinja_unrendered(
    mind: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Useful for inventory CLI: render with personae=None keeps the
    raw template strings (no Jinja exception). Strict YAML still
    quotes them, so pi/SDK still parse the file."""
    target = tmp_path / "state" / "speaking"
    registry = SkillRegistry.from_mind(mind, include_defaults=False)
    render_to_disk(
        registry, hemisphere="speaking", target_dir=target, personae=None
    )
    rendered_text = (target / ".claude" / "skills" / "log-meal" / "SKILL.md").read_text()
    assert "{{ user.name }}" in rendered_text
