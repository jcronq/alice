"""Jinja2-backed prompt loader with file resolution + override support.

The runtime ships default templates under
``src/alice_prompts/templates/``. A deployed mind can override any
template by dropping a same-named file at
``mind/.alice/prompts/<same-path>``; the override wins via Jinja's
:class:`ChoiceLoader` with the override path searched first.

Public surface:

- :class:`PromptLoader` — instantiated once per process. Plan 04
  Phase 1 has each entry point (wake, daemon factory) build its own;
  Phase 5 wires a singleton through ``alice_speaking.factory``.
- :class:`PromptNotFound` — raised when ``load(name, ...)`` can't find
  ``name`` in any search path.
- :func:`module-level load / list_prompts / reload` — convenience
  delegators on the package's default loader instance, for callers
  that don't need an override path. Plan 04 Phase 1 uses this from
  ``wake.py``; Phase 5 will replace with the daemon-built singleton.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Iterable, Optional

import jinja2


log = logging.getLogger(__name__)


class PromptNotFound(Exception):
    """Raised when :meth:`PromptLoader.load` can't find a template by
    name in any registered search path."""


# Templates use ``.md.j2`` so editors render them as markdown while
# tooling (and this loader) recognises the ``.j2`` half as Jinja.
TEMPLATE_SUFFIX = ".md.j2"


def _name_to_path(name: str) -> str:
    """Translate a dotted prompt name into a relative file path.

    ``"speaking.compact"`` → ``"speaking/compact.md.j2"``.
    ``"speaking.capability.signal"`` → ``"speaking/capability.signal.md.j2"``.

    The convention is: the FIRST dot separates the package from the
    template stem; subsequent dots stay in the file name. This lets
    naming groups like ``speaking.capability.signal`` /
    ``speaking.capability.cli`` co-locate under ``speaking/`` without
    a deeper directory.
    """
    if "." not in name:
        raise ValueError(
            f"prompt name must be dot-separated (got {name!r}); "
            "expected <package>.<stem>"
        )
    package, _, stem = name.partition(".")
    return f"{package}/{stem}{TEMPLATE_SUFFIX}"


class PromptLoader:
    """Render Jinja2 templates by dotted name, with override support.

    Resolution order:

    1. ``override_path`` (typically ``mind/.alice/prompts/``) —
       optional, the highest-priority search path. Operators drop
       customised templates here without touching the runtime
       package.
    2. ``defaults_path`` — the runtime defaults bundled with the
       wheel, ``src/alice_prompts/templates/``.

    Render context layers:

    1. ``context_defaults`` passed at construction (e.g. personae
       once Plan 05 lands).
    2. Per-call kwargs from :meth:`load`.

    Per-call values win on key collision.
    """

    def __init__(
        self,
        defaults_path: pathlib.Path,
        *,
        override_path: Optional[pathlib.Path] = None,
        context_defaults: Optional[dict[str, Any]] = None,
    ) -> None:
        if not defaults_path.is_dir():
            raise FileNotFoundError(
                f"prompt defaults directory missing: {defaults_path}"
            )
        self._defaults_path = defaults_path
        self._override_path = override_path
        self._context_defaults: dict[str, Any] = dict(context_defaults or {})
        self._env = self._build_env()

    def _build_env(self) -> jinja2.Environment:
        loaders: list[jinja2.BaseLoader] = []
        if self._override_path is not None and self._override_path.is_dir():
            loaders.append(jinja2.FileSystemLoader(str(self._override_path)))
        loaders.append(jinja2.FileSystemLoader(str(self._defaults_path)))
        return jinja2.Environment(
            loader=jinja2.ChoiceLoader(loaders),
            # Templates are mostly markdown destined for Claude; HTML
            # autoescaping would corrupt the prose. Keep autoescape
            # off and let templates pick their own escaping rules.
            autoescape=False,
            # Strip the trailing newline of `{% block %}` etc. so
            # rendered output doesn't pick up extra whitespace from
            # block markers — keep prompts visually identical to the
            # f-string originals they replace.
            trim_blocks=True,
            lstrip_blocks=True,
            # Surface unresolved placeholders as empty rather than
            # raising — Plan 05 wires personae into context_defaults;
            # before then, ``{{agent.name}}`` should render literally
            # via the StrictUndefined-equivalent we set below.
            undefined=jinja2.StrictUndefined,
        )

    # ------------------------------------------------------------------
    # Public API

    def load(self, name: str, /, **context: Any) -> str:
        """Render the named template with merged context.

        ``name`` is dot-separated; see :func:`_name_to_path`. The
        ``/`` makes ``name`` position-only so callers can pass a
        context kwarg literally called ``name`` without collision
        (e.g. ``load("speaking.hello", name="Owner")``). Raises
        :class:`PromptNotFound` when no template matches.
        """
        path = _name_to_path(name)
        try:
            template = self._env.get_template(path)
        except jinja2.TemplateNotFound as exc:
            raise PromptNotFound(
                f"prompt {name!r} not found (searched {path!r})"
            ) from exc
        merged = {**self._context_defaults, **context}
        return template.render(**merged)

    def list_prompts(self) -> list[str]:
        """Return all known prompt names (dotted form), de-duplicated
        across search paths. Useful for inventory tooling and the
        recurrence guards in tests."""
        names: set[str] = set()
        for path in self._search_paths():
            for tpl in self._discover(path):
                names.add(tpl)
        return sorted(names)

    def reload(self) -> None:
        """Re-discover templates after on-disk edits. The Jinja
        environment caches; rebuilding it picks up any new files
        added to either search path. Daemon hot-reload uses this."""
        self._env = self._build_env()

    # ------------------------------------------------------------------
    # Internals

    def _search_paths(self) -> Iterable[pathlib.Path]:
        if self._override_path is not None and self._override_path.is_dir():
            yield self._override_path
        yield self._defaults_path

    def _discover(self, root: pathlib.Path) -> Iterable[str]:
        for tpl_path in root.rglob(f"*{TEMPLATE_SUFFIX}"):
            rel = tpl_path.relative_to(root)
            # speaking/compact.md.j2 → speaking.compact
            stem = rel.with_suffix("").with_suffix("").as_posix()
            # First slash separates package; remaining slashes (if any)
            # become dots so listing matches the load() naming.
            yield stem.replace("/", ".")
