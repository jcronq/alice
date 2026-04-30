"""Phase M of plan 05: AST literal walker forbids hardcoded
``"Alice"`` / ``"owner"`` outside an explicit allowlist.

The point: code paths the agent eventually sees (tool descriptions,
system prompts, recipient examples) must derive identity from
:mod:`alice_core.config.personae` rather than baking in the
runtime's default. New offenses fail in CI before merge.

The walker is intentionally simple:

- ``ast.parse`` every ``.py`` under ``src/``.
- Walk every ``ast.Constant`` whose ``value`` is a string.
- Skip module / class / function docstrings (the first ``Expr``
  whose value is a string in those bodies — Python's documented
  docstring shape).
- Skip nodes inside an explicit ``ALLOWLIST_FILES`` set: files where
  the literal exists for a legitimate, audited reason (default
  fallback values, scaffold templates, etc.).

Comments aren't in the AST, so they don't trip the walker.
Identifiers + import names also don't appear as ``ast.Constant``
nodes — only string *literals* do.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = ("Alice", "owner", "Owner")


# Files where the literal is intentional + audited. Each entry is a
# repo-relative path under ``src/``. Keep this small + reviewed.
ALLOWLIST_FILES: frozenset[str] = frozenset(
    {
        # The placeholder personae is the source of the runtime default.
        "alice_core/config/personae.py",
        # The package-level prompt loader's stand-in personae — same
        # role, mirrored across packages so cold-imports stay clean.
        "alice_prompts/__init__.py",
        # principals.load() takes 'owner' as the legacy default fallback;
        # personae overrides it when supplied (Phase L).
        "alice_speaking/domain/principals.py",
        # A2A AgentCard's agent_name kwarg default — the daemon overrides
        # it from personae in production, but the SDK-side default must
        # remain a literal string.
        "alice_speaking/transports/a2a.py",
    }
)


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Collect ``id(node)`` for every Constant that is a docstring —
    Python's documented shape: the first statement in a Module /
    ClassDef / FunctionDef / AsyncFunctionDef body, where that
    statement is ``Expr(value=Constant(str))``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _violations_in(py_file: pathlib.Path) -> list[tuple[int, str]]:
    """Return ``(lineno, value)`` pairs for every offending literal in
    ``py_file`` (excluding docstrings)."""
    tree = ast.parse(py_file.read_text())
    docstring_ids = _docstring_constants(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in docstring_ids:
            continue
        if not any(token in node.value for token in FORBIDDEN):
            continue
        out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize(
    "py_file",
    _python_files(),
    ids=lambda p: p.relative_to(SRC_ROOT).as_posix(),
)
def test_no_hardcoded_personae_literals(py_file: pathlib.Path) -> None:
    rel = py_file.relative_to(SRC_ROOT).as_posix()
    if rel in ALLOWLIST_FILES:
        return
    violations = _violations_in(py_file)
    assert not violations, (
        f"{rel} contains hardcoded persona literals: "
        + ", ".join(f"line {ln}: {v!r}" for ln, v in violations[:5])
        + "\nUse alice_core.config.personae instead, or extend the "
        "allowlist in tests/test_no_hardcoded_personae_literals.py "
        "if the literal is genuinely a default fallback."
    )


def test_allowlist_files_actually_exist() -> None:
    """Pin: every entry in ALLOWLIST_FILES corresponds to a real file
    under ``src/``. So a refactor that moves a file shows up here as a
    test failure rather than silently letting the allowlist rot."""
    for rel in ALLOWLIST_FILES:
        assert (SRC_ROOT / rel).is_file(), (
            f"allowlist entry {rel!r} doesn't resolve under src/; "
            "remove or update the allowlist."
        )
