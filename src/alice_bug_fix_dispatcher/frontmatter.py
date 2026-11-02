"""YAML-ish frontmatter read / mutate / write for ``bug-N.md`` files.

The status site's own ``parse_frontmatter`` (status_site.server) is
read-only; this module adds the write path. We keep the format tight
and compatible with the renderer:

* Frontmatter is the leading ``---\\n...\\n---\\n`` block.
* Each line is ``key: value``. Values are plain strings; a value
  containing a colon, a leading / trailing quote, or shell-special
  characters is written double-quoted.
* Lines without a colon (or starting with ``#``) are passed through
  untouched when preserving unknown content.

Write path is atomic: ``mkstemp`` in the same directory, write,
``os.replace``. This matches the status site's ``.pages.json`` write
pattern, so a crash mid-write never leaves a torn frontmatter.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Mapping


_QUOTE_NEEDED_RE = re.compile(r"[:#\"'\\]")


def _needs_quoting(value: str) -> bool:
    """True when the value must be double-quoted to round-trip through parse_frontmatter."""
    if not value:
        return False
    if value[0].isspace() or value[-1].isspace():
        return True
    if _QUOTE_NEEDED_RE.search(value):
        return True
    return False


def _quote(value: str) -> str:
    """Return ``value`` wrapped in double quotes with ``"`` + ``\\`` escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_value(value: object) -> str:
    """Render a Python scalar back to a frontmatter value string."""
    if value is None:
        return ""
    s = str(value)
    if _needs_quoting(s):
        return _quote(s)
    return s


def parse(text: str) -> tuple[dict[str, str], str, list[str]]:
    """Parse ``---\\n...\\n---\\n`` + body.

    Returns ``(fields, body, raw_front_lines)``. ``fields`` is the
    key→value dict (same semantics as status_site.server.parse_frontmatter,
    but we also strip double-quote escaping on the way out).
    ``raw_front_lines`` is the exact list of lines between the two
    ``---`` fences, preserved so a round-trip rewrite can keep
    unknown lines (comments, non-key lines) untouched when it needs
    to.

    If the file has no frontmatter block, returns an empty dict +
    the original text as body + an empty list.
    """
    if not text.startswith("---\n"):
        return {}, text, []
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text, []
    front = text[4:end]
    body = text[end + len("\n---\n") :]
    raw_lines = front.splitlines()
    data: dict[str, str] = {}
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            # Unescape double-quoted values; single-quoted taken literal.
            inner = value[1:-1]
            if value[0] == '"':
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            value = inner
        data[key] = value
    return data, body, raw_lines


def render(fields: Mapping[str, object], body: str) -> str:
    """Render a ``---\\n...\\n---\\n<body>`` document.

    ``fields`` is written in insertion order. Values whose
    ``format_value`` is the empty string are dropped — a caller that
    wants to clear a field should delete the key from the mapping
    rather than set it to ``None``.
    """
    lines = ["---"]
    for key, value in fields.items():
        rendered = format_value(value)
        if rendered == "":
            continue
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    # Preserve the body's own leading newline shape. ``parse`` returns
    # ``body`` with its leading ``\n`` (because we split on
    # ``\n---\n``); joining with ``\n`` reconstructs the original
    # layout.
    return "\n".join(lines) + body if body.startswith("\n") else "\n".join(lines) + "\n" + body


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via tmp+rename.

    Uses ``mkstemp`` in the same directory as ``path`` so the final
    ``os.replace`` is atomic (same filesystem). The temp file inherits
    the default umask; after the rename the mode matches whatever
    ``path`` already had (``os.replace`` doesn't touch destination
    mode).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path_str, path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


def update(
    path: Path,
    patch: Mapping[str, object | None],
    *,
    remove_keys: tuple[str, ...] = (),
) -> dict[str, str]:
    """Read ``path``, apply ``patch`` to its frontmatter, write back.

    * Keys in ``patch`` whose value is ``None`` are dropped from the
      frontmatter.
    * Keys in ``patch`` whose value is a string are inserted (if new)
      or updated (if present). New keys append at the end of the
      block so the file's existing ordering (reporter-visible top
      fields like ``report_id``, ``title``, ``status``) stays stable.
    * ``remove_keys`` is a shortcut for "drop these keys unconditionally."
    * The body (everything after the second ``---``) is passed through
      unchanged.

    Returns the merged frontmatter dict after the write — useful for
    log lines ("flipped bug-2 to in-progress; dispatched_at=…").
    """
    text = path.read_text()
    fields, body, _raw = parse(text)
    merged: dict[str, object] = dict(fields)
    for key in remove_keys:
        merged.pop(key, None)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    out = render({k: v for k, v in merged.items()}, body)
    atomic_write(path, out)
    return {k: format_value(v) for k, v in merged.items()}
