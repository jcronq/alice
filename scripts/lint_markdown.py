#!/usr/bin/env python3
"""Markdown lint — frontmatter validation + optional markdownlint layer.

Used by the repo's ``.githooks/pre-commit`` to catch broken YAML frontmatter
before it lands on master. The motivating bug (2026-05-20): an unquoted
multi-word value containing colons ("naming_decision: 2026-05-20 09:43 EDT —
Jason picked …") slipped into alice-mind master because nothing validated
the frontmatter at commit time.

Usage:
    lint_markdown.py file1.md file2.md ...
    git diff --cached --name-only --diff-filter=ACMR | grep '\\.md$' | lint_markdown.py

Exits 0 on success, 1 on any lint failure. Non-markdown paths are silently
skipped (pipeline-friendly).

Checks:
  1. YAML frontmatter validity. If the file opens with ``---\\n``, parse the
     block between the opening and closing ``---`` with ``yaml.safe_load``.
     ``YAMLError`` → fail with the file path and the line number from the
     parser's ``problem_mark``.
  2. markdownlint (best-effort layer 2). If ``markdownlint-cli2`` or
     ``markdownlint`` is on PATH, run it. If absent, print a one-line note
     and continue — we don't want the hook to be unreachable on machines
     that lack node.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "lint_markdown: PyYAML is required (pip install pyyaml).\n"
    )
    sys.exit(2)


FRONTMATTER_DELIM = "---"


def iter_paths(argv: list[str]) -> Iterable[Path]:
    """Yield candidate paths from argv or stdin (one per line)."""
    if argv:
        raw = argv
    else:
        raw = [line.strip() for line in sys.stdin if line.strip()]
    for p in raw:
        yield Path(p)


def extract_frontmatter(text: str) -> tuple[str, int] | None:
    """Return (frontmatter_text, body_start_line) or None if no frontmatter.

    A frontmatter block starts with a line containing exactly ``---`` and
    ends at the next line containing exactly ``---``. Returns the inner text
    (without the delimiters) plus the 1-based line number where the body
    begins (useful for downstream line offsets).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_DELIM:
            inner = "\n".join(lines[1:idx])
            return inner, idx + 2  # body begins on line after closing ---
    # Opening delimiter but no closing delimiter — treat as a frontmatter
    # error rather than silently passing.
    return "\n".join(lines[1:]), len(lines) + 1


def lint_frontmatter(path: Path) -> list[str]:
    """Return a list of error strings for the file (empty == clean)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: cannot read file ({exc})"]

    block = extract_frontmatter(text)
    if block is None:
        return []  # no frontmatter is fine — not all .md files need it

    inner, _body_start = block
    try:
        yaml.safe_load(inner)
    except yaml.YAMLError as exc:
        # PyYAML attaches a problem_mark on most parser errors. Line numbers
        # in problem_mark are 0-based and relative to the YAML block, so we
        # add 2 (1 for the opening --- line, 1 for 1-based indexing).
        line_hint = ""
        problem_mark = getattr(exc, "problem_mark", None)
        if problem_mark is not None:
            file_line = problem_mark.line + 2
            line_hint = f" at line {file_line}"
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return [
            f"{path}: YAML frontmatter invalid{line_hint}: {problem}.\n"
            f"  Hint: values that contain ':' must be quoted "
            f"(e.g. `key: \"value with: colon\"`)."
        ]

    return []


def maybe_markdownlint(paths: list[Path]) -> tuple[int, str]:
    """Run markdownlint-cli2 or markdownlint if available.

    Returns (exit_code, note). exit_code==0 means pass-or-skipped; the note
    is a short status line to print. We don't fail the commit when the
    binary is missing — the YAML check is the must-have, markdownlint is
    bonus.
    """
    binary = shutil.which("markdownlint-cli2") or shutil.which("markdownlint")
    if binary is None:
        return 0, (
            "lint_markdown: markdownlint not installed — skipping style "
            "checks. Install with `npm i -g markdownlint-cli2` for richer "
            "checks."
        )
    if not paths:
        return 0, ""
    cmd = [binary, *[str(p) for p in paths]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        return 0, f"lint_markdown: markdownlint invocation failed ({exc}); skipping."
    if result.returncode != 0:
        out = (result.stdout + result.stderr).strip()
        return result.returncode, f"markdownlint:\n{out}"
    return 0, "lint_markdown: markdownlint clean."


def main(argv: list[str]) -> int:
    paths = [
        p for p in iter_paths(argv)
        if p.suffix.lower() in (".md", ".markdown") and p.is_file()
    ]
    if not paths:
        return 0

    errors: list[str] = []
    for path in paths:
        errors.extend(lint_frontmatter(path))

    if errors:
        sys.stderr.write("lint_markdown: frontmatter errors:\n")
        for err in errors:
            sys.stderr.write(f"  {err}\n")
        sys.stderr.write(
            "\nFix the frontmatter and re-stage. To bypass for a known-good "
            "edge case, run with --no-verify (NOT recommended).\n"
        )
        return 1

    md_code, md_note = maybe_markdownlint(paths)
    if md_note:
        # Markdownlint output goes to stderr; the YAML pass goes to stdout
        # so the hook stays quiet on the happy path.
        sys.stderr.write(md_note + "\n")
    if md_code != 0:
        return md_code

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
