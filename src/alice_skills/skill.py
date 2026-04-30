"""Skill dataclass + SKILL.md frontmatter parser.

A skill is a directory under ``.claude/skills/`` containing a
``SKILL.md`` with YAML frontmatter. Required frontmatter: ``name``
+ ``description``. Optional: ``scope`` (``speaking`` / ``thinking``
/ ``both``; default ``both``), ``ops`` (declared composite ops).

Composite skills (``cortex-memory``) ship sub-procedure markdown
files under ``ops/``. Phase 1 models ops as nested :class:`Skill`
instances on the parent's ``ops`` tuple — without their own
frontmatter requirement, so they match the existing on-disk shape.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


SkillScope = Literal["speaking", "thinking", "both"]
_VALID_SCOPES: frozenset[str] = frozenset({"speaking", "thinking", "both"})


class SkillError(ValueError):
    """Raised on missing required frontmatter, invalid YAML, or
    unknown scope. Message names the offending file + field."""


# YAML frontmatter sits between two ``---`` markers at the top of
# the file. We deliberately don't import pyyaml at module load —
# parsing is lazy + small.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _strip_frontmatter(text: str) -> tuple[str, str]:
    """Split a SKILL.md into ``(frontmatter_yaml, body)``. If no
    frontmatter is present, returns ``("", text)``."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ("", text)
    return (m.group(1), text[m.end():])


def _parse_frontmatter(yaml_body: str, *, source: pathlib.Path) -> Mapping[str, Any]:
    """Parse the YAML between ``---`` markers. Empty frontmatter →
    empty mapping. Strict YAML is tried first; on failure we fall
    back to a forgiving line-based parser because real-world skill
    descriptions routinely contain unquoted colons (e.g. ``"lunch:
    X"``) that strict YAML rejects. Skill authors aren't writing
    YAML — they're writing markdown frontmatter."""
    if not yaml_body.strip():
        return {}
    import yaml as _yaml

    try:
        data = _yaml.safe_load(yaml_body)
    except _yaml.YAMLError:
        return _parse_frontmatter_lenient(yaml_body, source=source)
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise SkillError(
            f"{source}: frontmatter must be a YAML mapping (got "
            f"{type(data).__name__})"
        )
    return data


def _parse_frontmatter_lenient(
    yaml_body: str, *, source: pathlib.Path
) -> Mapping[str, Any]:
    """Forgiving fallback: ``key: value`` per line, value is
    everything after the first colon. Multi-line continuations
    (lines starting with whitespace) append to the previous key.
    Bool-ish values normalize to ``True`` / ``False``.

    This handles the common case where a user-authored skill
    description has unquoted colons. The cost: nested mappings
    aren't supported here (skills don't currently use them).
    """
    out: dict[str, Any] = {}
    current_key: str | None = None
    for line in yaml_body.splitlines():
        if not line.strip():
            current_key = None
            continue
        if line.startswith((" ", "\t")) and current_key is not None:
            # Continuation of the previous key's value.
            existing = out[current_key]
            if isinstance(existing, str):
                out[current_key] = (existing + " " + line.strip()).strip()
            continue
        if ":" not in line:
            current_key = None
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        out[key] = _coerce_scalar(value)
        current_key = key
    if not out:
        raise SkillError(
            f"{source}: invalid frontmatter (no parseable key: value lines)"
        )
    return out


def _coerce_scalar(value: str) -> Any:
    """Normalize the trivial scalar shapes a skill author might use."""
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    return value


@dataclass(frozen=True)
class Skill:
    """One skill loaded from disk.

    ``description_template`` holds the raw frontmatter string. Phase 4
    adds :meth:`describe_for` that renders it through the personae
    context — the rendered string can swap in different ``user.name``
    values without re-parsing the SKILL.md.

    ``ops`` is the tuple of nested ops (sub-procedures under
    ``ops/`` of a composite skill like ``cortex-memory``). Ops do
    not require frontmatter; their ``description_template`` may be
    empty.
    """

    name: str
    description_template: str
    body: str
    source_path: pathlib.Path
    scope: SkillScope = "both"
    fires_in_quiet_hours: bool = True
    emit_telemetry: bool = True
    ops: tuple["Skill", ...] = ()
    raw_frontmatter: Mapping[str, Any] = field(default_factory=dict)

    @property
    def description(self) -> str:
        """Backwards-compatible: returns the raw template string.

        Phase 4 callers should use :meth:`describe_for` to render
        against the active personae. Kept as the raw template so
        existing inventory code (``bin/alice-skills list``) keeps
        working without a personae argument.
        """
        return self.description_template

    def describe_for(self, personae: Any) -> str:
        """Render the description against ``personae`` (Plan 07 Phase 4).

        Uses Jinja2 with the same ``{{ agent }}`` / ``{{ user }}``
        context shape as :class:`alice_prompts.PromptLoader` so a
        skill description can interpolate ``{{ user.name }}`` /
        ``{{ agent.name }}`` and pick up the operator's configured
        values.
        """
        if "{{" not in self.description_template:
            return self.description_template
        import jinja2

        env = jinja2.Environment(
            autoescape=False,
            undefined=jinja2.StrictUndefined,
        )
        template = env.from_string(self.description_template)
        ctx = personae.as_template_context() if personae is not None else {}
        return template.render(**ctx)

    @classmethod
    def parse(cls, skill_md: pathlib.Path) -> "Skill":
        """Load a top-level ``SKILL.md`` (and any sibling ``ops/``).

        ``skill_md`` must point at the SKILL.md inside a skill
        directory; the skill's name is the directory name unless
        the frontmatter explicitly overrides ``name``. Required:
        ``description``. ``scope`` defaults to ``both``.
        """
        text = skill_md.read_text()
        yaml_body, body = _strip_frontmatter(text)
        fm = _parse_frontmatter(yaml_body, source=skill_md)

        name = fm.get("name") or skill_md.parent.name
        if not isinstance(name, str) or not name.strip():
            raise SkillError(
                f"{skill_md}: 'name' must be a non-empty string"
            )

        desc = fm.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise SkillError(
                f"{skill_md}: 'description' is required (non-empty string)"
            )

        scope_raw = fm.get("scope", "both")
        if scope_raw not in _VALID_SCOPES:
            raise SkillError(
                f"{skill_md}: scope = {scope_raw!r}; expected one of "
                f"{sorted(_VALID_SCOPES)}"
            )
        scope: SkillScope = scope_raw  # type: ignore[assignment]

        fires_in_quiet = bool(fm.get("fires_in_quiet_hours", True))
        emit_telemetry = bool(fm.get("emit_telemetry", True))

        ops_dir = skill_md.parent / "ops"
        ops_list: list[Skill] = []
        if ops_dir.is_dir():
            for op_path in sorted(ops_dir.glob("*.md")):
                ops_list.append(_parse_op(op_path, parent_scope=scope))

        return cls(
            name=name.strip(),
            description_template=desc,
            body=body,
            source_path=skill_md,
            scope=scope,
            fires_in_quiet_hours=fires_in_quiet,
            emit_telemetry=emit_telemetry,
            ops=tuple(ops_list),
            raw_frontmatter=dict(fm),
        )


def _parse_op(op_path: pathlib.Path, *, parent_scope: SkillScope) -> Skill:
    """Parse a sub-procedure file under ``<skill>/ops/<op>.md``.

    Ops don't require frontmatter; absent fields fall through to the
    parent's scope + sensible defaults. This matches today's
    on-disk shape (cortex-memory's ops have plain markdown bodies).
    """
    text = op_path.read_text()
    yaml_body, body = _strip_frontmatter(text)
    fm = _parse_frontmatter(yaml_body, source=op_path)

    name = fm.get("name") or op_path.stem
    if not isinstance(name, str) or not name.strip():
        raise SkillError(f"{op_path}: 'name' must be a non-empty string")

    desc = fm.get("description") or _first_nonempty_line(body)
    if not isinstance(desc, str):
        raise SkillError(f"{op_path}: 'description' must be a string if present")

    scope_raw = fm.get("scope", parent_scope)
    if scope_raw not in _VALID_SCOPES:
        raise SkillError(
            f"{op_path}: scope = {scope_raw!r}; expected one of "
            f"{sorted(_VALID_SCOPES)}"
        )
    scope: SkillScope = scope_raw  # type: ignore[assignment]

    return Skill(
        name=name.strip(),
        description_template=desc,
        body=body,
        source_path=op_path,
        scope=scope,
        ops=(),
        raw_frontmatter=dict(fm),
    )


def _first_nonempty_line(body: str, cap: int = 200) -> str:
    """Default op description: first non-empty heading-stripped line
    of the body. Keeps inventory tooling readable when the op file
    has no explicit ``description:``."""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:cap]
    return ""
