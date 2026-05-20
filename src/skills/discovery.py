"""Filesystem walking for skill directories.

A skill is a directory under one of the search paths containing a
``SKILL.md``. This module walks the search paths in priority order,
yields ``(name, path_to_SKILL.md)`` tuples, and lets the registry
resolve overrides (mind-side wins over runtime defaults).
"""

from __future__ import annotations

import pathlib
from typing import Iterator


__all__ = ["DEFAULTS_DIR", "DEFAULT_SEARCH_PATHS", "iter_skill_paths"]


# Runtime-default skills ship with this package (Plan 07 Phase 6).
# Resolved relative to this file so the package works installed
# (from the wheel) and editable (from the repo).
DEFAULTS_DIR: pathlib.Path = pathlib.Path(__file__).resolve().parent / "defaults"


# Conventional search paths in priority order: mind > .alice (post-
# refactor location) > runtime defaults. The factory passes a
# concrete list at registry-construction time; this constant exists
# so callers without a mind dir (the inventory CLI tests) have a
# reasonable default.
DEFAULT_SEARCH_PATHS: tuple[pathlib.Path, ...] = (DEFAULTS_DIR,)


def iter_skill_paths(
    search_paths: list[pathlib.Path],
) -> Iterator[tuple[str, pathlib.Path]]:
    """Walk each search path; yield ``(skill_name, SKILL.md path)``
    in priority order — earlier search paths shadow later ones for
    the same skill name.

    Skill name is the parent directory name. We don't use the
    frontmatter's ``name:`` for shadowing because the directory
    layout is what the SDK's auto-loader sees on disk.
    """
    seen: set[str] = set()
    for root in search_paths:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            name = entry.name
            if name in seen:
                continue  # earlier search path already supplied this skill
            seen.add(name)
            yield (name, skill_md)
