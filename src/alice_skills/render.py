"""Render filtered skills to a per-hemisphere ephemeral directory.

Plan 07 Phase 3 + plan-pi Phase C.

The runtime renders the in-scope subset of a mind's skills into
``<state_dir>/alice-skills/<hemisphere>/`` at startup, and the
kernel's ``cwd`` points at that directory. This achieves three
things:

1. **Hemisphere scope enforcement.** Speaking sees only
   ``scope: speaking|both``; thinking sees only ``scope:
   thinking|both``. The SDK auto-loader (and pi-coding-agent's
   discovery) walks ``.claude/skills/`` under cwd; out-of-scope
   skills literally aren't on disk.
2. **Personae substitution.** ``{{ user.name }}`` /
   ``{{ agent.name }}`` Jinja in descriptions + bodies is rendered
   against the active :class:`Personae` so the model sees concrete
   values rather than literal template strings.
3. **Strict-YAML frontmatter.** Re-emit frontmatter via PyYAML's
   ``default_style='"'`` so every scalar is double-quoted —
   protects against unquoted-colon-inside-quote-string descriptions
   (the ``"lunch: X"`` case that breaks pi's strict YAML parser).

The render is atomic at the directory level: write a sibling
``.tmp`` directory, then rename. Stale state from a previous deploy
with different scoping doesn't survive.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from typing import Any, Optional

from .registry import SkillRegistry
from .skill import Skill, SkillScope


__all__ = ["render_to_disk"]


def render_to_disk(
    registry: SkillRegistry,
    *,
    hemisphere: SkillScope,
    target_dir: pathlib.Path,
    personae: Optional[Any] = None,
    mind_dir: Optional[pathlib.Path] = None,
) -> list[Skill]:
    """Render in-scope skills to ``<target_dir>/.claude/skills/``.

    ``hemisphere`` is one of ``"speaking"``, ``"thinking"``, or
    ``"both"`` (the last returns every skill — useful for
    inventory CLI; not what kernel cwd points at).

    ``personae`` is the :class:`alice_core.config.personae.Personae`
    used to render Jinja in descriptions + bodies. ``None`` skips
    Jinja rendering — useful for tests + the inventory CLI.

    ``mind_dir`` is the mind directory; if its ``CLAUDE.md`` exists
    it gets copied into ``<target_dir>/CLAUDE.md`` so the SDK's
    auto-loader still finds it after the cwd swap.

    Returns the list of rendered skills (the ones that actually
    landed on disk).
    """
    in_scope = registry.for_hemisphere(hemisphere)

    target_dir = pathlib.Path(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Build into a sibling .tmp dir, then rename. Sequential rather
    # than os.replace-atomic, but the worker is the only reader and
    # restarts pick up the swap cleanly.
    tmp = target_dir.parent / f".{target_dir.name}.tmp.{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp_skills_dir = tmp / ".claude" / "skills"
    tmp_skills_dir.mkdir(parents=True, exist_ok=True)

    for skill in in_scope:
        skill_target = tmp_skills_dir / skill.name
        skill_target.mkdir(parents=True)

        rendered = _render_skill_md(skill, personae)
        (skill_target / "SKILL.md").write_text(rendered)

        # Copy known sub-directories verbatim. Non-SKILL.md root
        # files (README.md etc.) are intentionally NOT copied —
        # pi's discovery rule treats them as candidate skills and
        # fails on missing frontmatter.
        src_dir = skill.source_path.parent
        for sub in ("ops", "scripts", "references", "assets"):
            sub_src = src_dir / sub
            if sub_src.is_dir():
                shutil.copytree(sub_src, skill_target / sub)

    if mind_dir is not None:
        claude_md = pathlib.Path(mind_dir) / "CLAUDE.md"
        if claude_md.is_file():
            shutil.copy2(claude_md, tmp / "CLAUDE.md")

    if target_dir.exists():
        shutil.rmtree(target_dir)
    tmp.rename(target_dir)

    return in_scope


def _render_skill_md(skill: Skill, personae: Optional[Any]) -> str:
    """Render a SKILL.md with Jinja-substituted frontmatter (description)
    + body, then re-emit frontmatter as strict-YAML double-quoted
    scalars."""
    import yaml as _yaml

    fm = dict(skill.raw_frontmatter)
    fm["name"] = skill.name
    # Preserve raw Jinja templates when personae is unavailable
    # (inventory tooling, tests). The runtime path always passes
    # personae; pi/SDK parsing of unrendered templates still works
    # because the strict-YAML quoting handles literal "{{ ... }}".
    fm["description"] = (
        skill.describe_for(personae)
        if personae is not None
        else skill.description_template
    )

    body = skill.body
    if personae is not None and "{{" in body:
        import jinja2

        env = jinja2.Environment(
            autoescape=False, undefined=jinja2.StrictUndefined
        )
        body = env.from_string(body).render(**personae.as_template_context())

    yaml_text = _yaml.dump(
        fm,
        default_flow_style=False,
        default_style='"',
        sort_keys=False,
        allow_unicode=True,
    )
    return f"---\n{yaml_text}---\n{body}"
