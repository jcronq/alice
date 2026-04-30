"""Alice's prompts: one canonical directory of templates, one loader.

Plan 04 of the runtime refactor moves every prompt the runtime sends
out of Python source and into ``.md.j2`` templates under
:mod:`alice_prompts.templates`. The loader handles per-mind
overrides, Jinja2 substitution, and inventory listing.

Module-level ``load`` / ``list_prompts`` / ``reload`` delegate to a
default :class:`PromptLoader` instance pointed at the runtime
defaults only (no override path). This is convenient for callers
that don't need a per-mind override (the wake hemisphere, tests,
the inventory CLI). The speaking daemon constructs its own loader
via :func:`alice_speaking.factory.build_registry` once Plan 04
Phase 5 lands; that loader is wired with the deployed mind's
override path.
"""

from __future__ import annotations

import pathlib
from typing import Any

from .loader import PromptLoader, PromptNotFound


__all__ = [
    "DEFAULTS_DIR",
    "PromptLoader",
    "PromptNotFound",
    "default_loader",
    "list_prompts",
    "load",
    "reload",
    "set_default_loader",
]


# The runtime defaults ship under ``src/alice_prompts/templates/``;
# resolve relative to this file so the package works installed (from
# the wheel) and editable (from the repo).
DEFAULTS_DIR: pathlib.Path = pathlib.Path(__file__).resolve().parent / "templates"


_default_loader: PromptLoader | None = None


def default_loader() -> PromptLoader:
    """Lazy-construct the package-level loader pointed at defaults
    only. Idempotent; subsequent calls return the same instance.

    The daemon path doesn't use this — it builds its own loader with
    the mind's override path injected. This singleton is for the
    callers that don't have an override (wake.py at Phase 1, tests,
    the inventory CLI).

    The ``context_defaults`` here include placeholder personae so
    templates with ``{{agent.name}}`` / ``{{user.name}}`` render
    sensibly until Plan 05 wires real personae from the mind. The
    placeholders are intentionally generic — Plan 05 replaces them.
    """
    global _default_loader
    if _default_loader is None:
        _default_loader = PromptLoader(
            defaults_path=DEFAULTS_DIR,
            context_defaults=_persona_placeholder_defaults(),
        )
    return _default_loader


def _persona_placeholder_defaults() -> dict[str, Any]:
    """Stand-in personae values used until Plan 05 lands real ones.

    Templates already reference ``{{agent.name}}`` / ``{{user.name}}``
    so the substitution surface is in place; once Plan 05 wires
    actual personae into the loader's context_defaults, this
    function retires.
    """
    return {
        "agent": {"name": "Alice"},
        "user": {"name": "the operator"},
    }


def load(name: str, /, **context: Any) -> str:
    """Render ``name`` with the package-level loader."""
    return default_loader().load(name, **context)


def list_prompts() -> list[str]:
    """Return every prompt name known to the package-level loader."""
    return default_loader().list_prompts()


def reload() -> None:
    """Re-discover templates on the package-level loader."""
    default_loader().reload()


def set_default_loader(loader: PromptLoader) -> None:
    """Replace the package-level singleton with a custom loader.

    Used by :func:`alice_speaking.factory.build_prompt_loader` so the
    daemon's mind-override path applies to every call site that uses
    the convenience ``alice_prompts.load(...)``. Idempotent: calling
    again replaces the previous override.
    """
    global _default_loader
    _default_loader = loader
