"""Alice skills — first-class registry over ``.claude/skills/`` markdown.

Plan 07 of the runtime refactor lifts skill discovery out of the
Claude Code SDK's auto-loader into a package the runtime owns. The
agent still sees skills via the SDK's auto-discovery (the SDK reads
``<cwd>/.claude/skills/``); the registry exists so the runtime can:

- Enumerate skills (``alice-skills list``).
- Render descriptions through the personae context (``{{ user.name }}``
  substitutes the configured user's name).
- Filter by hemisphere scope (``speaking`` / ``thinking`` / ``both``).
- Resolve runtime defaults overlaid by the mind's own skills.

This package ships Phases 1, 2, 4, 6 of the plan; Phase 3 (write
filtered skills to a per-hemisphere ephemeral dir so the SDK's
auto-loader sees the scope-correct set), Phase 5 (telemetry), and
Phase 7 (matching test harness) are deferred follow-ups.
"""

from __future__ import annotations

from .discovery import (
    DEFAULTS_DIR,
    DEFAULT_SEARCH_PATHS,
    iter_skill_paths,
)
from .registry import SkillRegistry
from .skill import (
    Skill,
    SkillError,
    SkillScope,
)


__all__ = [
    "DEFAULTS_DIR",
    "DEFAULT_SEARCH_PATHS",
    "Skill",
    "SkillError",
    "SkillRegistry",
    "SkillScope",
    "iter_skill_paths",
]
