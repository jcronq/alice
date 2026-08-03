"""YAML-lint-safe frontmatter serialization helpers for vault writes.

The vault's pre-commit hook (``scripts/lint_markdown.py``) validates every
frontmatter block with ``yaml.safe_load``. Prior to this module the vault
serializers scattered across memory-worker (``stage_b`` / ``stage_c`` /
``stage_d`` / ``correction_cascade_auto_propagate``) plus ``phase`` each
built frontmatter with bare ``f"{k}: {v}"`` string interpolation. Three
bug classes reliably produced YAML-invalid output (task-0617, incident
2026-07-27 → 2026-08-02 — six days of blocked autopush, 6000+ files
sitting in the working tree):

1. **Unquoted scalar values containing an internal ``:``.** YAML parses
   the second colon as a nested mapping. Example that broke:
   ``title: Meta-research: decay mechanism & scoring``.
2. **Trailing scalar glued to the closing ``---`` fence.** When an LLM-
   authored ``new_value`` for a diff is written verbatim through
   ``_serialize_frontmatter``, a missing terminating newline collapses
   the last value onto the closing fence: ``access_count: 8---``.
3. **Empty list entries emitted as bare ``-`` markers.** LLM-produced
   block-style list values sometimes end with a stray dash; passing the
   value through unfiltered leaves a bare ``-`` between the last item
   and the closing fence.

The helpers here are conservative: ``quote_if_needed`` only quotes when
the raw string would trigger a ``YAMLError`` OR reparse as a mapping
(the multi-colon trap). Dates, wikilinks, comma-strings, and integers
are still emitted unquoted so the vault's diffs stay readable.

``render_frontmatter`` is the batteries-included entry point. The
lower-level helpers (``quote_if_needed``, ``filter_list_items``,
``render_kv``) let existing serializer sites keep their layout
conventions (flow-style vs block-style lists, key ordering).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import yaml


# Chars that, when present inside a flow-list item, would confuse the
# flow-list parser (comma splits items, brackets nest, braces open flow
# mappings). Any list item containing one gets quoted before it lands
# inside a ``[a, b, c]`` rendering.
_FLOW_UNSAFE_CHARS = frozenset(",[]{}")


def quote_if_needed(value: str) -> str:
    """Return ``value`` quoted iff emitting it bare would break YAML.

    The check is *lint-safety*, not type-preservation: a bare
    ``2026-05-08`` reparses as a date object rather than the string
    ``"2026-05-08"``, but the lint accepts both. We only quote when
    the raw string would raise ``YAMLError`` or reparse as a nested
    mapping (the multi-colon bug pattern).
    """
    if not isinstance(value, str):
        return str(value)
    try:
        parsed = yaml.safe_load(f"__k: {value}")
    except yaml.YAMLError:
        return _yaml_quote(value)
    if not isinstance(parsed, dict) or "__k" not in parsed:
        return _yaml_quote(value)
    inner = parsed["__k"]
    # If the value reparsed to a dict, YAML saw an unintended nested
    # mapping — the multi-colon trap. Quote to force scalar semantics.
    if isinstance(inner, dict):
        return _yaml_quote(value)
    return value


def _yaml_quote(value: str) -> str:
    """Wrap ``value`` in double quotes with the minimum escaping needed.

    We prefer double-quoted style over single-quoted so embedded single
    quotes (common in prose titles) don't need doubling. Backslashes
    and double-quotes inside the value are escaped.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def filter_list_items(items: Iterable[Any]) -> list[Any]:
    """Drop ``None`` and whitespace-only string entries from a list.

    The stray-``-`` bug (bug 3 in task-0617) was reproducible when a
    list value contained a trailing empty string that then rendered as
    ``  - `` (or, worse, a bare ``-`` at column 0). Filtering here is
    the structural defense.
    """
    out: list[Any] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str) and not item.strip():
            continue
        out.append(item)
    return out


def _render_scalar_for_flow(value: Any) -> str:
    """Render an item destined for a flow-list ``[a, b, c]`` rendering.

    Individual flow-list items must not contain unescaped commas or
    brackets — those punctuation-split the list. Anything containing
    :data:`_FLOW_UNSAFE_CHARS` gets quoted; the rest deferrs to
    :func:`quote_if_needed`.
    """
    if not isinstance(value, str):
        return quote_if_needed(str(value))
    if any(c in _FLOW_UNSAFE_CHARS for c in value):
        return _yaml_quote(value)
    return quote_if_needed(value)


def render_kv(
    key: str,
    value: Any,
    *,
    list_style: str = "flow",
) -> list[str]:
    """Render one ``(key, value)`` pair as a list of frontmatter lines.

    ``list_style`` picks between ``"flow"`` (``key: [a, b]``) and
    ``"block"`` (``key:\\n  - a\\n  - b``). Existing serializer sites
    use both — the caller picks per its convention. ``None`` values
    (and empty list items) are filtered out, matching the historical
    behavior of ``_serialize_frontmatter``.
    """
    if value is None:
        return []
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    if isinstance(value, list):
        items = filter_list_items(value)
        if list_style == "block":
            if not items:
                # Preserve the historical block-style shape for an empty
                # list — a bare ``key:`` line — so a later re-parse
                # still sees the key present.
                return [f"{key}:"]
            lines = [f"{key}:"]
            for item in items:
                # Use the same guard as flow rendering so items starting
                # with ``[`` (wikilinks) or containing YAML-hostile chars
                # get quoted, otherwise ``- [[a]]`` reparses as a nested
                # list of ``[["a"]]`` instead of the string ``"[[a]]"``.
                lines.append(f"  - {_render_scalar_for_flow(item)}")
            return lines
        rendered_items = [_render_scalar_for_flow(v) for v in items]
        return [f"{key}: [{', '.join(rendered_items)}]"]
    if isinstance(value, str):
        return [f"{key}: {quote_if_needed(value)}"]
    return [f"{key}: {value}"]


def render_frontmatter(
    fm: Mapping[str, Any],
    *,
    preferred_keys: Iterable[str] = (),
    list_style: str = "flow",
) -> str:
    """Render a full frontmatter block including the ``---`` fences.

    Output shape: ``---\\n<lines>\\n---\\n``. ``preferred_keys`` emit
    first in the given order; remaining keys follow in insertion order.
    Always terminates with a newline before the closing fence
    (structural defense against bug 2 of task-0617).
    """
    lines: list[str] = ["---"]
    seen: set[str] = set()
    for key in preferred_keys:
        if key in fm:
            lines.extend(render_kv(key, fm[key], list_style=list_style))
            seen.add(key)
    for key, val in fm.items():
        if key in seen:
            continue
        lines.extend(render_kv(key, val, list_style=list_style))
    lines.append("---")
    return "\n".join(lines) + "\n"
