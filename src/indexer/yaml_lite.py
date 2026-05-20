"""Minimal YAML frontmatter parser for Obsidian-style markdown vaults.

Handles the subset of YAML actually used in vault frontmatter:
  - scalar strings (quoted or unquoted)
  - flow-style lists: [a, b, c]
  - block-style lists:
        key:
          - item
          - item
  - dates as YYYY-MM-DD (kept as strings)
  - integers (kept as ints when bare)

Not a full YAML parser. Stdlib-only — no pyyaml dependency, so the indexer
runs in any Python 3 environment without venv ceremony.

Returns a dict[str, str | int | list[str]].
"""

from __future__ import annotations

import re
from typing import Any


_FENCE = "---"


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body).

    If no frontmatter fence is present, returns ({}, text).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    return parse_frontmatter(fm_text), body


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the body between --- fences. Tolerant; unknown shapes become strings."""
    out: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        # top-level key: must be `key:` or `key: value` with no leading whitespace
        m = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).rstrip()
        if rest == "":
            # block-style list or empty value
            block_items: list[str] = []
            j = i + 1
            while j < len(lines):
                lj = lines[j]
                if not lj.strip():
                    j += 1
                    continue
                if not lj.startswith(" ") and not lj.startswith("\t"):
                    break
                stripped = lj.lstrip()
                if stripped.startswith("- "):
                    block_items.append(_unquote(stripped[2:].strip()))
                else:
                    # nested mapping; ignore content but consume
                    pass
                j += 1
            if block_items:
                out[key] = block_items
            else:
                out[key] = ""
            i = j
            continue
        # inline value
        out[key] = _parse_scalar(rest)
        i += 1
    return out


def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "":
        return ""
    # flow list
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        # split on commas not inside quotes (vault's flow lists are simple — basic split is fine)
        parts = _split_flow(inner)
        return [_unquote(p.strip()) for p in parts]
    # quoted scalar
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    # int
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except ValueError:
            pass
    # bool
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "~"):
        return ""
    return s


def _unquote(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    # Obsidian-style wikilink in a list item: [[target]] or "[[target]]"
    return s


def _split_flow(inner: str) -> list[str]:
    """Split flow-list inner content on commas not inside [[..]] or quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(inner):
        c = inner[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
        elif c in ('"', "'"):
            quote = c
            buf.append(c)
        elif c == "[":
            depth += 1
            buf.append(c)
        elif c == "]":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


# wikilink extraction
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\[\]]*?)?\]\]")
_FENCE_RE = re.compile(r"^(```|~~~)", re.MULTILINE)
# Inline code spans of varying tick width. Markdown allows N backticks to
# delimit a span containing fewer-tick runs, so we strip widest-first.
# A non-greedy body avoids spilling across paragraphs.
_DOUBLE_BACKTICK_RE = re.compile(r"``[^\n]*?``")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# A wikilink target that "looks like a slug" — lowercase alphanumerics,
# dashes, underscores. Optional folder prefix. Crucially excludes spaces
# and quotes, which is what differentiates a real wikilink reference
# (``[[2026-05-11-foo]]``) from a bash test expression
# (``[[ -d "$x" ]]``). Used to rescue backtick-wrapped wikilinks from
# the strip pass without re-admitting bash false positives.
_SLUG_LIKE_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]*$")


def _strip_code(body: str) -> str:
    """Remove fenced code blocks and inline code so [[..]] inside them isn't
    matched as a wikilink. Bash `[[ -d "$x" ]]`, markdown examples like
    `[[wikilinks]]`, and double-backtick spans like ``[[wikilink]]`` would
    otherwise pollute the broken-link queue.
    """
    # Strip fenced blocks (```...``` or ~~~...~~~). State-machine over lines.
    out_lines = []
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence is None and (stripped.startswith("```") or stripped.startswith("~~~")):
            fence = stripped[:3]
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        out_lines.append(line)
    cleaned = "\n".join(out_lines)
    # Strip wider runs first so inner narrower runs don't half-consume them.
    cleaned = _DOUBLE_BACKTICK_RE.sub("", cleaned)
    cleaned = _INLINE_CODE_RE.sub("", cleaned)
    return cleaned


def _rescue_backtick_wikilinks(body: str) -> list[str]:
    """Recover ``[[slug]]`` references that live inside inline backtick
    spans. ``_strip_code`` removes the entire span body — necessary for
    suppressing bash ``[[ -d ]]`` test expressions, but it also drops
    legitimate slug references that daily entries commonly format as
    `` `[[note-slug]]` ``. Those references should count toward the
    target note's inbound link total.

    Strategy: scan the body's backtick-span contents (before they get
    stripped) for ``[[..]]`` patterns, but only yield targets that look
    like slugs (lowercase alphanumerics + dashes + underscores + slash;
    no spaces, no quotes). The slug-like filter is what keeps bash
    expressions out — ``[[ -d $x ]]`` contains spaces and ``$``, so it
    never matches ``_SLUG_LIKE_RE``. Targets are still cross-validated
    against ``by_slug`` by the caller, so any non-existent slug-shaped
    target is harmless.
    """
    targets: list[str] = []
    for span_re in (_DOUBLE_BACKTICK_RE, _INLINE_CODE_RE):
        for span in span_re.findall(body):
            for m in _WIKILINK_RE.finditer(span):
                target = m.group(1).strip()
                # strip section anchors
                if "#" in target:
                    target = target.split("#", 1)[0].strip()
                if target and _SLUG_LIKE_RE.match(target):
                    targets.append(target)
    return targets


def extract_wikilinks(body: str, *, rescue_inline: bool = True) -> list[str]:
    """Return raw wikilink targets (display alias stripped). May contain folder/ prefixes.

    ``rescue_inline`` controls whether slug-shaped wikilinks that live inside
    inline backtick spans are recovered. The default (``True``) preserves the
    behaviour the indexer + orphan/inbound-link metrics rely on: a daily entry
    that references `` `[[note-slug]]` `` should still count as an inbound
    link, so the target isn't falsely flagged as orphaned.

    The broken-wikilink metric passes ``rescue_inline=False`` because the
    contract there is stricter — ``[[foo]]`` inside any code span (single
    backtick, double backtick, or a fenced block) is documentation, not a
    real reference, and must not contribute to the broken-link count.
    """
    cleaned = _strip_code(body)
    targets = []
    for m in _WIKILINK_RE.finditer(cleaned):
        target = m.group(1).strip()
        # strip section anchors: [[note#section]] → note
        if "#" in target:
            target = target.split("#", 1)[0].strip()
        if target:
            targets.append(target)
    if rescue_inline:
        # Rescue slug-shaped wikilinks that lived inside inline code spans
        # (e.g. `` `[[note-slug]]` `` in daily entries). Without this, those
        # references go missing and target notes appear orphaned.
        targets.extend(_rescue_backtick_wikilinks(body))
    return targets
