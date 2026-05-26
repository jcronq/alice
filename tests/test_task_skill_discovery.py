"""Confirm the ``task`` skill is discoverable as a scope=both default.

Issue #375 ships the skill via the existing
``src/skills/defaults/`` directory so both Speaking (Claude SDK) and
Thinking (pi-coding-agent) see it through their shared cwd-walk
discovery contract.
"""

from __future__ import annotations

from skills import DEFAULTS_DIR, SkillRegistry


def test_task_skill_is_registered_with_scope_both() -> None:
    registry = SkillRegistry.from_search_paths([DEFAULTS_DIR])
    skill = registry.find("task")
    assert skill is not None, (
        f"`task` skill missing from {DEFAULTS_DIR}; SKILL.md not loading?"
    )
    assert skill.scope == "both"


def test_task_skill_visible_in_both_hemispheres() -> None:
    registry = SkillRegistry.from_search_paths([DEFAULTS_DIR])
    speaking_names = {s.name for s in registry.for_hemisphere("speaking")}
    thinking_names = {s.name for s in registry.for_hemisphere("thinking")}
    assert "task" in speaking_names
    assert "task" in thinking_names


def test_task_skill_description_mentions_inner_tasks() -> None:
    """Sanity check on the SKILL.md body — the description is what the
    LLM sees in the skill chooser, so it must mention the store."""
    registry = SkillRegistry.from_search_paths([DEFAULTS_DIR])
    skill = registry.find("task")
    assert skill is not None
    # The skill description points at the SM v2 task store
    assert "task" in skill.description.lower()
    # The body documents the CLI
    assert "task create" in skill.body
    assert "task list" in skill.body
