#!/usr/bin/env python3
"""Normalize invalid ``trigger_keywords`` frontmatter to YAML flow sequences.

A backfill-era writer (no longer in the tree) emitted ``trigger_keywords``
as a bare double-quoted CSV::

    trigger_keywords: "a", "b", "c"

That is not valid YAML. Both readers fail on it:

* ``indexer.yaml_lite._parse_scalar`` sees a value that starts and ends
  with ``"`` and returns the inner string, so ``isinstance(v, list)`` is
  False and the indexer records ``trigger_count = 0``.
* ``cue_runner._TRIGGER_KEYWORDS_RE`` requires ``[...]`` brackets and
  silently yields ``[]``.

Either way the note loses the +1.5x trigger-keyword re-rank boost. This
script rewrites the offending line to a proper flow sequence::

    trigger_keywords: [a, b, c]

and drops garbage tokens the backfill emitted (JSON/colon fragments,
truncated wikilinks, markdown punctuation, empties).

**Execution boundary:** Speaking authors this; the memory worker runs it
(``--apply``) as part of its frontmatter-normalize responsibility. Speaking
runs ``--dry-run`` only (read-only) to verify. After ``--apply``, the FTS
index rebuilds on its normal cadence and picks up the corrected lists.

Usage::

    python3 scripts/normalize_trigger_keywords.py --dry-run [--vault PATH]
    python3 scripts/normalize_trigger_keywords.py --apply   [--vault PATH]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Matches a trigger_keywords line whose value is a bare quoted CSV
# (starts with a quote, no leading ``[``). Indented variants allowed.
_BAD_LINE_RE = re.compile(r'^(\s*)trigger_keywords:\s*(["\'].*)$')

# A keyword is "garbage" if it carries structural punctuation the backfill
# leaked in: JSON/dict braces, colons, brackets/wikilink fragments,
# backticks, markdown heading marks, stray quotes, or is empty.
_GARBAGE_CHARS = set('{}[]:#`"')


def _split_quoted_csv(value: str) -> list[str]:
    """Split ``"a", "b", "c"`` into ``[a, b, c]``.

    Walks character by character, tracking the active quote char, so an
    escaped or stray inner quote doesn't fracture a token. Returns the
    unquoted, stripped pieces.
    """
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in value:
        if quote is None:
            if ch in ('"', "'"):
                quote = ch
            elif ch == ',':
                token = "".join(buf).strip()
                if token:
                    items.append(token)
                buf = []
            # commas/space outside quotes are separators; ignore other text
        else:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
    token = "".join(buf).strip()
    if token:
        items.append(token)
    return items


def _is_clean(kw: str) -> bool:
    """True if ``kw`` is a usable keyword (no structural punctuation)."""
    kw = kw.strip()
    if not kw:
        return False
    if any(c in _GARBAGE_CHARS for c in kw):
        return False
    # Drop tokens that are pure punctuation / ellipsis remnants.
    if not any(c.isalnum() for c in kw):
        return False
    return True


def _render_flow(keywords: list[str]) -> str:
    """Render ``[a, b, c]``. Keywords here are already clean and
    bracket/quote-free, so a bare flow sequence is valid YAML."""
    return "[" + ", ".join(keywords) + "]"


def normalize_line(line: str) -> tuple[str, list[str]] | None:
    """If ``line`` is a malformed trigger_keywords line, return the
    rewritten line plus the dropped garbage tokens. Else ``None``."""
    m = _BAD_LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    indent, value = m.group(1), m.group(2)
    raw = _split_quoted_csv(value)
    clean = [k for k in raw if _is_clean(k)]
    dropped = [k for k in raw if not _is_clean(k)]
    # Dedupe, preserve order.
    seen: set[str] = set()
    deduped: list[str] = []
    for k in clean:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            deduped.append(k)
    new_line = f"{indent}trigger_keywords: {_render_flow(deduped)}\n"
    return new_line, dropped


def _iter_markdown(vault: Path):
    for path in vault.rglob("*.md"):
        if "/.handled/" in str(path) or "/.consumed/" in str(path):
            continue
        yield path


def process_file(path: Path, *, apply: bool) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines(keepends=True)
    # Only touch the frontmatter block (between the first two ``---``).
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None

    changed = False
    dropped_all: list[str] = []
    for i in range(1, end):
        result = normalize_line(lines[i])
        if result is None:
            continue
        new_line, dropped = result
        if new_line != lines[i]:
            lines[i] = new_line
            changed = True
            dropped_all.extend(dropped)
    if not changed:
        return None
    if apply:
        path.write_text("".join(lines), encoding="utf-8")
    return {"path": str(path), "dropped": dropped_all}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report, write nothing")
    g.add_argument("--apply", action="store_true", help="rewrite files in place")
    ap.add_argument(
        "--vault",
        default=os.path.expanduser("~/alice-mind/cortex-memory"),
        help="vault root to scan (default: ~/alice-mind/cortex-memory)",
    )
    args = ap.parse_args(argv)

    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    touched = 0
    dropped_total = 0
    for path in _iter_markdown(vault):
        res = process_file(path, apply=args.apply)
        if res is None:
            continue
        touched += 1
        dropped_total += len(res["dropped"])
        rel = os.path.relpath(res["path"], str(vault))
        if res["dropped"]:
            print(f"{rel}: normalized, dropped {res['dropped']}")
        else:
            print(f"{rel}: normalized")

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] {touched} notes normalized, {dropped_total} garbage tokens dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
