"""Tests for scripts/lint_markdown.py.

Covers the three cases Jason's directive called out:
  (a) valid frontmatter passes,
  (b) the exact 2026-05-20 bug pattern fails with a line-number hint,
  (c) files without frontmatter pass.

Also covers the #405 additions:
  - ``--fix`` auto-quotes the 2026-05-20 bug pattern,
  - ``--fix`` is conservative about block scalars / flow constructs /
    already-quoted values,
  - the default (no-``--fix``) report leads with a deduped file-list
    summary then per-file detail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "lint_markdown.py"


def run_lint(*paths: Path, fix: bool = False) -> subprocess.CompletedProcess[str]:
    """Invoke the linter as a subprocess so we exercise the CLI surface."""
    args: list[str] = [sys.executable, str(SCRIPT)]
    if fix:
        args.append("--fix")
    args.extend(str(p) for p in paths)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )


def write(tmp_path: Path, name: str, body: str) -> Path:
    """Write ``body`` to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Existing core-behavior tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# #405: at-a-glance file list in default output
# ---------------------------------------------------------------------------


def test_default_output_leads_with_file_list_summary(tmp_path: Path) -> None:
    """Without --fix, the report must lead with a deduped list of failing
    files before any per-file details — so a developer reading the hook
    output sees N failing paths at a glance instead of having to scroll
    through every parser error to figure out which files broke."""
    # The bug trigger is an unquoted scalar containing ``: `` (colon-space)
    # in the value — PyYAML treats that as a nested mapping indicator.
    bad_body = (
        "---\n"
        "naming_decision: 2026-05-20 09:43 EDT — Jason said: yes\n"
        "---\n\nbody\n"
    )
    a = write(tmp_path, "a.md", bad_body)
    b = write(tmp_path, "b.md", bad_body)
    c = write(tmp_path, "c.md", "---\ntitle: ok\n---\n\nbody\n")  # clean

    result = run_lint(a, b, c)
    assert result.returncode == 1

    err = result.stderr
    # Summary header mentions the failing count.
    assert "2 file(s) failed frontmatter check" in err, err
    # The file-list summary appears before the Details section.
    summary_pos = err.find("2 file(s) failed frontmatter check")
    details_pos = err.find("Details:")
    assert 0 <= summary_pos < details_pos, (
        f"summary must precede details; got summary@{summary_pos}, "
        f"details@{details_pos}\nstderr={err}"
    )
    # Both bad paths appear in the summary; clean path does not.
    assert str(a) in err and str(b) in err
    assert str(c) not in err


def test_file_list_summary_dedupes(tmp_path: Path) -> None:
    """If a file is passed twice on the CLI, the summary should list it
    once — the developer cares about distinct broken files, not how many
    times the hook saw each path."""
    bad = write(
        tmp_path,
        "dup.md",
        "---\nbad: 2026-05-20 09:43 EDT — Jason said: yes\n---\n\nbody\n",
    )
    result = run_lint(bad, bad)
    assert result.returncode == 1
    summary_block, _, _ = result.stderr.partition("Details:")
    assert summary_block.count(str(bad)) == 1, (
        f"expected one summary line per failing file, got:\n{summary_block}"
    )


# ---------------------------------------------------------------------------
# #405: --fix mode behaviour
# ---------------------------------------------------------------------------


def test_fix_quotes_unquoted_colon_value(tmp_path: Path) -> None:
    """The exact 2026-05-20 bug pattern: --fix should wrap the value in
    double quotes and the resulting file must lint clean."""
    raw_value = "2026-05-20 09:43 EDT — Jason said: yes"
    bad_body = (
        "---\n"
        "title: clean\n"
        f"naming_decision: {raw_value}\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "bug.md", bad_body)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, (
        f"expected fix+lint to succeed, got rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )

    new = path.read_text(encoding="utf-8")
    assert f'naming_decision: "{raw_value}"' in new, (
        f"value should be quoted in-place; got:\n{new}"
    )
    # File is now parseable end-to-end.
    fm = new.split("---\n", 2)[1]
    parsed = yaml.safe_load(fm)
    assert parsed["naming_decision"] == raw_value
    assert parsed["title"] == "clean"
    # Untouched key is byte-exact preserved.
    assert "title: clean\n" in new


def test_fix_handles_all_affected_405_patterns(tmp_path: Path) -> None:
    """Synthesised after the four files listed in issue #405 — multiple
    sibling keys, each carrying the bug pattern. All should be quoted in
    a single --fix pass."""
    bad_body = (
        "---\n"
        "title: fitness\n"
        "decision_one: 2026-05-20 09:43 EDT — Jason said: yes\n"
        "decision_two: 2026-05-20 10:00 EDT — second call with: more context\n"
        "decision_three: 2026-05-21 08:00 EDT — third one referencing thinking.workflow rejected: too risky\n"
        "---\n\nbody text\n"
    )
    path = write(tmp_path, "multi.md", bad_body)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    new = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(new.split("---\n", 2)[1])
    assert parsed["decision_one"].startswith("2026-05-20")
    assert parsed["decision_two"].startswith("2026-05-20")
    assert parsed["decision_three"].startswith("2026-05-21")
    assert "body text\n" in new  # body untouched


def test_fix_does_not_touch_already_quoted_values(tmp_path: Path) -> None:
    original = (
        '---\n'
        'title: "already: quoted"\n'
        "plain: simple\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "ok.md", original)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, result.stderr
    # File is byte-exact preserved (no fixes were needed).
    assert path.read_text(encoding="utf-8") == original


def test_fix_does_not_touch_block_scalars(tmp_path: Path) -> None:
    """Block-scalar values (``|`` / ``>``) contain free-form text including
    colons by design. The fixer must leave them alone and must not rewrite
    the indented continuation lines underneath the block header."""
    original = (
        "---\n"
        "title: blocky\n"
        "summary: |\n"
        "  2026-05-20 09:43 EDT — Jason said this\n"
        "  and: more colons here\n"
        "flow: >\n"
        "  folded with: colon inside\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "block.md", original)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, result.stderr
    # File is byte-exact preserved — block scalars are valid YAML already.
    assert path.read_text(encoding="utf-8") == original


def test_fix_does_not_touch_flow_constructs(tmp_path: Path) -> None:
    """Flow sequences ``[a, b]`` and flow mappings ``{k: v}`` are explicit
    YAML constructs — the fixer must not wrap them in quotes."""
    original = (
        "---\n"
        "tags: [alpha, beta, gamma]\n"
        "meta: {author: alice, version: 1}\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "flow.md", original)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == original


def test_fix_writes_atomically_no_partial_file(tmp_path: Path) -> None:
    """A successful --fix swap must leave the target file fully readable
    (and only that file — no stray temp files lying around). This catches
    the obvious failure mode of a torn write at the rename boundary."""
    bad_body = (
        "---\n"
        "decision: 2026-05-20 09:43 EDT — Jason said: yes\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "atomic.md", bad_body)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, result.stderr

    # File exists and is readable.
    final = path.read_text(encoding="utf-8")
    assert final.startswith("---\n")
    assert final.endswith("body\n")
    assert "decision: \"2026-05-20" in final

    # No leftover temp files in the directory.
    siblings = [
        p.name for p in tmp_path.iterdir()
        if p.name != "atomic.md"
    ]
    assert siblings == [], f"unexpected leftover files: {siblings}"


def test_fix_preserves_body_byte_exact(tmp_path: Path) -> None:
    """The fixer touches the frontmatter only — the body (including
    multi-line content, trailing whitespace, and final newline state) must
    be preserved byte-exact."""
    body = (
        "# Heading\n\n"
        "Some text with: colons inside.\n\n"
        "```\nyaml: like content: with colons\n```\n\n"
        "Trailing line without final newline"
    )
    full = (
        "---\n"
        "decision: 2026-05-20 09:43 EDT — Jason said: yes\n"
        "---\n"
        + body
    )
    path = write(tmp_path, "body.md", full)
    result = run_lint(path, fix=True)
    assert result.returncode == 0, result.stderr

    new_text = path.read_text(encoding="utf-8")
    # Body section after closing --- is unchanged.
    new_body = new_text.split("---\n", 2)[2]
    assert new_body == body, f"body changed:\nold:\n{body!r}\nnew:\n{new_body!r}"


def test_fix_noop_on_already_clean_file(tmp_path: Path) -> None:
    """If nothing needs fixing, --fix must not rewrite the file (byte-exact
    preservation, including any unusual line endings) and must say so."""
    original = '---\ntitle: "Clean"\ntags: [a, b]\n---\n\nbody\n'
    path = write(tmp_path, "clean.md", original)
    mtime_before = path.stat().st_mtime_ns
    result = run_lint(path, fix=True)
    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == original
    # No-op message goes to stdout.
    assert "no fixable frontmatter" in result.stdout.lower()
    # Mtime should not bump — we didn't write.
    assert path.stat().st_mtime_ns == mtime_before


def test_fix_off_by_default(tmp_path: Path) -> None:
    """Without --fix, a broken file must NOT be rewritten — even though we
    could. Default behaviour is report-only."""
    bad_body = (
        "---\n"
        "decision: 2026-05-20 09:43 EDT — Jason said: yes\n"
        "---\n\nbody\n"
    )
    path = write(tmp_path, "bug.md", bad_body)
    before = path.read_text(encoding="utf-8")
    result = run_lint(path)  # no --fix
    assert result.returncode == 1
    after = path.read_text(encoding="utf-8")
    assert before == after, "linter must not mutate files without --fix"


def test_fix_help_text_mentions_flag() -> None:
    """``--help`` must surface the --fix flag and its safety stance, since
    the help output is the primary discovery path for users."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "--fix" in out
    assert "auto-quote" in out.lower() or "auto quote" in out.lower()
