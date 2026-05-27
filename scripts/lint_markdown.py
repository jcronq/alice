#!/usr/bin/env python3
"""Markdown lint — frontmatter validation + optional markdownlint layer.

Used by the repo's ``.githooks/pre-commit`` to catch broken YAML frontmatter
before it lands on master. The motivating bug (2026-05-20): an unquoted
multi-word value containing colons ("naming_decision: 2026-05-20 09:43 EDT —
Jason picked …") slipped into alice-mind master because nothing validated
the frontmatter at commit time.

Usage:
    lint_markdown.py file1.md file2.md ...
    lint_markdown.py --fix file1.md file2.md ...
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

Flags:
  --fix   Attempt to auto-quote unquoted top-level frontmatter scalar
          values that contain an internal colon (the 2026-05-20 bug
          pattern). The fixer is intentionally conservative — anything it
          isn't sure about is left alone with a warning. Atomic write
          (temp file + ``os.replace``) so a torn write can't half-corrupt
          a file. Off by default; default behaviour is report-only.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

# Top-level mapping entry inside frontmatter: `key: value` with at least one
# space after the colon. Group names are used by the --fix path so the
# substitution preserves indentation and separator exactly.
_KEY_VALUE_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)"
    r"(?P<sep>:[ \t]+)"
    r"(?P<value>.+?)"
    r"[ \t]*$"
)

# A value starting with any of these is something we deliberately don't
# touch in --fix mode: already quoted, a block scalar, a flow construct,
# or an anchor/alias/tag.
_SKIP_VALUE_PREFIXES = ('"', "'", "|", ">", "[", "{", "&", "*", "!", "#")

# A pure block-scalar header (e.g. ``|``, ``>``, ``|-``, ``>2``).
_BLOCK_SCALAR_RE = re.compile(r"^[|>][-+0-9]*$")


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


def _try_quote_value(value: str) -> str | None:
    """Return a safely double-quoted form of ``value``, or None to skip.

    Conservative: refuses anything that already starts with a YAML-special
    indicator, contains a literal ``"`` (would need escape handling beyond
    a simple wrap), or contains a backslash (same reason). The result is
    re-parsed via ``yaml.safe_load`` and must round-trip to the original
    string — otherwise we'd be silently changing the value.
    """
    stripped = value.strip()
    if not stripped:
        return None
    if stripped[0] in _SKIP_VALUE_PREFIXES:
        return None
    if '"' in stripped or "\\" in stripped:
        return None
    quoted = f'"{stripped}"'
    try:
        parsed = yaml.safe_load(quoted)
    except yaml.YAMLError:
        return None
    if parsed != stripped:
        return None
    return quoted


def _frontmatter_end_index(lines: list[str]) -> int | None:
    """Return the index of the closing ``---`` line, or None if absent."""
    if not lines or lines[0].rstrip("\r\n").strip() != FRONTMATTER_DELIM:
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").strip() == FRONTMATTER_DELIM:
            return i
    return None


def fix_frontmatter_text(text: str) -> tuple[str, list[str], list[str]]:
    """Attempt to auto-quote unquoted scalars containing colons.

    Returns ``(new_text, fixes_applied, warnings)``. ``new_text == text``
    when nothing changed (either the frontmatter already parses, no fixable
    candidates were found, or the candidate set did not resolve the parse
    error).

    Operates at the line level — never reformats untouched lines, never
    reorders keys, never strips comments. Only the specific lines we
    quote are rewritten; everything else is byte-exact preserved.
    """
    block = extract_frontmatter(text)
    if block is None:
        return text, [], []

    inner, _ = block
    try:
        yaml.safe_load(inner)
        return text, [], []  # already clean
    except yaml.YAMLError:
        pass

    lines = text.splitlines(keepends=True)
    end_idx = _frontmatter_end_index(lines)
    if end_idx is None:
        return text, [], ["unterminated frontmatter — refusing to fix"]

    # Pass 1: collect candidate replacements. We track whether we're inside
    # a block-scalar continuation (lines indented under a ``key: |`` header)
    # so we don't accidentally rewrite body content of a literal block.
    candidates: list[tuple[int, str]] = []
    warnings: list[str] = []
    in_block_scalar = False
    block_scalar_indent = -1

    for i in range(1, end_idx):
        raw = lines[i]
        if raw.endswith("\r\n"):
            content, nl = raw[:-2], "\r\n"
        elif raw.endswith("\n"):
            content, nl = raw[:-1], "\n"
        else:
            content, nl = raw, ""

        if in_block_scalar:
            indent_len = len(content) - len(content.lstrip(" "))
            if not content.strip() or indent_len > block_scalar_indent:
                continue
            in_block_scalar = False

        stripped_line = content.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue

        m = _KEY_VALUE_RE.match(content)
        if not m:
            continue

        value = m.group("value")
        stripped_value = value.strip()

        if _BLOCK_SCALAR_RE.match(stripped_value):
            in_block_scalar = True
            block_scalar_indent = len(m.group("indent"))
            continue

        if not stripped_value or stripped_value[0] in _SKIP_VALUE_PREFIXES:
            continue
        if ":" not in stripped_value:
            continue

        quoted = _try_quote_value(value)
        if quoted is None:
            warnings.append(
                f"line {i + 1}: unquoted value of `{m.group('key')}` "
                f"looks suspect but is not safely auto-quotable; "
                f"leaving alone"
            )
            continue

        new_line = (
            m.group("indent") + m.group("key") + m.group("sep") + quoted + nl
        )
        candidates.append((i, new_line))

    if not candidates:
        return text, [], warnings

    # Pass 2: apply all candidates, then validate. If the whole frontmatter
    # parses after the substitutions, accept; otherwise abandon all fixes
    # (better to fail loud than silently rewrite a file we don't understand).
    working = list(lines)
    fixes: list[str] = []
    for i, new_line in candidates:
        working[i] = new_line
        fixes.append(f"line {i + 1}: quoted value")

    candidate_inner = "".join(working[1:end_idx])
    # extract_frontmatter strips trailing newline; mirror that for parsing.
    candidate_inner_for_parse = candidate_inner.rstrip("\n")
    try:
        yaml.safe_load(candidate_inner_for_parse)
    except yaml.YAMLError as exc:
        return text, [], warnings + [
            f"attempted {len(candidates)} fix(es) but YAML still "
            f"invalid: {exc}; no changes written"
        ]

    return "".join(working), fixes, warnings


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in same dir + replace).

    Uses ``os.replace`` so the swap is atomic on POSIX. The temp file is
    fsynced before rename so a crash mid-write cannot leave a half-written
    file with the final path. Mode/owner of the original file are not
    preserved here — the pre-commit context doesn't need them, and the
    files in question are all checked into git.
    """
    parent = path.parent if str(path.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def fix_file(path: Path) -> tuple[bool, list[str], list[str]]:
    """Run the auto-fixer against ``path``. Returns (changed, fixes, warnings)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, [], [f"{path}: cannot read file ({exc})"]

    new_text, fixes, warnings = fix_frontmatter_text(text)
    if new_text == text or not fixes:
        return False, [], warnings

    try:
        _atomic_write(path, new_text)
    except OSError as exc:
        return False, [], warnings + [f"{path}: write failed ({exc})"]
    return True, fixes, warnings


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lint_markdown",
        description="Validate YAML frontmatter in markdown files.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Markdown files to check. If empty, paths are read from stdin "
        "(one per line). Non-markdown paths are skipped.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-quote unquoted top-level scalar values that contain an "
        "internal colon (the 2026-05-20 bug pattern). Conservative: "
        "skips block scalars, flow sequences/mappings, already-quoted "
        "values, and anything containing a literal '\"' or '\\\\'. "
        "Writes atomically (temp file + os.replace). Off by default.",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    paths = [
        p for p in iter_paths(args.paths)
        if p.suffix.lower() in (".md", ".markdown") and p.is_file()
    ]
    if not paths:
        return 0

    if args.fix:
        any_changed = False
        for path in paths:
            changed, fixes, warnings = fix_file(path)
            if changed:
                any_changed = True
                sys.stdout.write(f"lint_markdown: fixed {path}\n")
                for fix in fixes:
                    sys.stdout.write(f"    {fix}\n")
            for warning in warnings:
                sys.stderr.write(f"lint_markdown: {warning}\n")
        if not any_changed:
            sys.stdout.write("lint_markdown: no fixable frontmatter found.\n")
        # Re-run the validator after fixing so we surface anything we
        # couldn't repair. Anything still failing exits non-zero so the
        # caller (and CI) knows manual intervention is required.

    errors_by_file: dict[Path, list[str]] = {}
    for path in paths:
        file_errors = lint_frontmatter(path)
        if file_errors:
            errors_by_file[path] = file_errors

    if errors_by_file:
        # Lead with a deduplicated, at-a-glance file list so a developer
        # reading the hook output sees the N failing paths immediately
        # before scrolling through per-file details.
        failing = sorted({str(p) for p in errors_by_file})
        sys.stderr.write(
            f"lint_markdown: {len(failing)} file(s) failed frontmatter check:\n"
        )
        for path in failing:
            sys.stderr.write(f"  {path}\n")
        sys.stderr.write("\nDetails:\n")
        for path in sorted(errors_by_file, key=str):
            for err in errors_by_file[path]:
                sys.stderr.write(f"  {err}\n")
        sys.stderr.write(
            "\nFix the frontmatter and re-stage. Try `lint_markdown.py --fix "
            "<file>` to auto-quote simple cases. To bypass for a known-good "
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
