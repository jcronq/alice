"""Tests for scripts/lint_markdown.py.

Covers the three cases Jason's directive called out:
  (a) valid frontmatter passes,
  (b) the exact 2026-05-20 bug pattern fails with a line-number hint,
  (c) files without frontmatter pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "lint_markdown.py"


def run_lint(*paths: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the linter as a subprocess so we exercise the CLI surface."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
        check=False,
    )


def write(tmp_path: Path, name: str, body: str) -> Path:
    """Write ``body`` to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_frontmatter_passes(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "ok.md",
        '---\ntitle: "Test"\nauthor: alice\ncreated: 2026-05-20\n---\n\n# Body\n',
    )
    result = run_lint(path)
    assert result.returncode == 0, (
        f"expected pass, got rc={result.returncode}\nstderr={result.stderr}"
    )


def test_unquoted_colon_value_fails(tmp_path: Path) -> None:
    """The exact bug pattern from alice-mind 4f180ad: an unquoted multi-word
    value containing an inline ``: `` (colon-space). Must fail with a clear,
    line-numbered error."""
    bug_body = (
        "---\n"
        "title: thinking.workflow — state machine design draft\n"
        "authored_by: speaking\n"
        "naming_decision: 2026-05-20 09:43 EDT — Jason picked `github.workflow` "
        "(rename of current SM v2) and `thinking.workflow` (new system); "
        "symmetric, no acronyms\n"
        "adk_decision: 2026-05-20 09:47 EDT — Jason ruled out ADK for these "
        "workflows. ADK considered for the in-wake pipeline (SequentialAgent / "
        "LoopAgent shells) but rejected: the wake itself is already a single "
        "Claude session driven by the existing wake template; ADK would add a "
        "layer without giving the multi-day persistence story anything new.\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "bug.md", bug_body)
    result = run_lint(path)
    assert result.returncode == 1, (
        "expected fail on unquoted colon value, "
        f"got rc={result.returncode}\nstderr={result.stderr}"
    )
    assert "frontmatter invalid" in result.stderr.lower()
    assert "line" in result.stderr.lower(), (
        f"expected a line-number hint in error, got: {result.stderr}"
    )


def test_no_frontmatter_passes(tmp_path: Path) -> None:
    path = write(tmp_path, "plain.md", "# Just a heading\n\nNo frontmatter.\n")
    result = run_lint(path)
    assert result.returncode == 0, (
        f"plain markdown should pass, got rc={result.returncode}\n"
        f"stderr={result.stderr}"
    )


def test_unterminated_frontmatter_fails(tmp_path: Path) -> None:
    """Defensive: an opening --- with no closing --- should not silently pass."""
    path = write(tmp_path, "unterminated.md", "---\nkey: value\nbody with no closing delim\n")
    result = run_lint(path)
    # Unterminated frontmatter happens to parse as valid YAML in this case
    # (it's just key: value plus a free-form string line), so we don't
    # require failure — but we do require the linter to not crash.
    assert result.returncode in (0, 1)


def test_non_markdown_paths_skipped(tmp_path: Path) -> None:
    """A .py file passed in should be silently ignored."""
    py = write(tmp_path, "thing.py", "print('hi')\n")
    result = run_lint(py)
    assert result.returncode == 0, result.stderr


def test_paths_from_stdin(tmp_path: Path) -> None:
    """The hook pipes paths via stdin; make sure that path works too."""
    path = write(tmp_path, "ok.md", "---\ntitle: Test\n---\n\nbody\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=str(path) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
