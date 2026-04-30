"""SkillRegistry — the runtime's source of truth for skills.

Construction:

    registry = SkillRegistry.from_search_paths(
        [mind_dir / ".claude" / "skills",
         mind_dir / ".alice" / "skills",
         DEFAULTS_DIR],
    )

Lookups:

    registry.all()                       # every skill, post-override
    registry.find("log-meal")            # by name
    registry.for_hemisphere("speaking")  # scope-filtered

Phase 1 of Plan 07 ships the registry; Phase 3 (deferred) will
extend it with the on-disk write step that mirrors the filtered
set into a per-hemisphere ephemeral directory the SDK auto-loads.
Until then, ``for_hemisphere`` returns a Python list — useful for
inventory + telemetry but the SDK's auto-loader still sees every
on-disk skill.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Iterable

from .discovery import DEFAULTS_DIR, iter_skill_paths
from .skill import Skill, SkillError, SkillScope


__all__ = ["SkillRegistry"]


log = logging.getLogger(__name__)


class SkillRegistry:
    """Loaded set of :class:`Skill` instances with override resolution
    + hemisphere scoping.

    ``search_paths`` is a list of directories in priority order
    (earliest wins). Skills are loaded once at construction; call
    :meth:`reload` to re-walk after on-disk edits.
    """

    def __init__(self, skills: Iterable[Skill], *, search_paths: list[pathlib.Path]):
        self._skills: list[Skill] = list(skills)
        self._search_paths = list(search_paths)
        self._by_name: dict[str, Skill] = {s.name: s for s in self._skills}

    # ------------------------------------------------------------------
    # Construction

    @classmethod
    def from_search_paths(
        cls, search_paths: list[pathlib.Path]
    ) -> "SkillRegistry":
        """Walk each path, parse every SKILL.md, return a registry.

        Parse errors propagate (don't silently drop skills) — the
        inventory CLI catches and reports them; the speaking +
        thinking factories let them surface so a malformed skill
        is visible at deploy.
        """
        loaded: list[Skill] = []
        for _, skill_md in iter_skill_paths(search_paths):
            loaded.append(Skill.parse(skill_md))
        return cls(loaded, search_paths=search_paths)

    @classmethod
    def from_mind(
        cls,
        mind_dir: pathlib.Path | str,
        *,
        include_defaults: bool = True,
    ) -> "SkillRegistry":
        """Convenience: search the mind's standard skill paths +
        optionally the runtime defaults.

        Priority order (highest first):

        1. ``mind/.claude/skills/`` (existing convention; the SDK
           auto-loader's expected location).
        2. ``mind/.alice/skills/`` (Plan 07-aware future location).
        3. :data:`alice_skills.discovery.DEFAULTS_DIR` (runtime
           defaults shipped with the package).
        """
        mind = pathlib.Path(mind_dir)
        paths: list[pathlib.Path] = [
            mind / ".claude" / "skills",
            mind / ".alice" / "skills",
        ]
        if include_defaults:
            paths.append(DEFAULTS_DIR)
        return cls.from_search_paths(paths)

    # ------------------------------------------------------------------
    # Public lookups

    def all(self) -> list[Skill]:
        """Every skill, post-override resolution."""
        return list(self._skills)

    def find(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    def for_hemisphere(self, scope: SkillScope) -> list[Skill]:
        """Return skills whose declared scope matches ``scope`` or
        is ``"both"``.

        ``scope`` must be ``"speaking"`` or ``"thinking"``; passing
        ``"both"`` returns every skill (matches the registry's
        ``all()``).
        """
        if scope == "both":
            return self.all()
        return [s for s in self._skills if s.scope in (scope, "both")]

    def is_skill_path(self, path: pathlib.Path) -> bool:
        """True if ``path`` matches any registered skill's
        ``source_path`` (or one of its ops). Phase 5 telemetry uses
        this to decide whether a ``Read`` tool call counts as a
        skill load — see plan 07 §"Telemetry"."""
        target = path.resolve(strict=False)
        for s in self._skills:
            if s.source_path.resolve(strict=False) == target:
                return True
            for op in s.ops:
                if op.source_path.resolve(strict=False) == target:
                    return True
        return False

    def reload(self) -> None:
        """Re-walk the search paths. Daemon hot-reload uses this when
        a SKILL.md changes on disk."""
        loaded: list[Skill] = []
        for _, skill_md in iter_skill_paths(self._search_paths):
            try:
                loaded.append(Skill.parse(skill_md))
            except SkillError:
                log.exception("skill parse failed during reload: %s", skill_md)
                continue
        self._skills = loaded
        self._by_name = {s.name: s for s in loaded}
