"""Phase 3 of plan 08: dependency-direction CI guard.

``alice_core/`` is the kernel: it depends on Python stdlib, the
Claude Agent SDK, and itself — nothing else. The day a sibling
package leaks in (the obvious risk: a ``KernelSpec`` field typed
as ``Personae`` from ``alice_speaking``), this test fails before
merge.

The check: walk every ``.py`` under ``src/alice_core/``, AST-parse
the import statements, assert the top-level module of every import
is in the allow-list. Stdlib is computed via
``sys.stdlib_module_names`` (Python 3.10+). The allow-list adds
``alice_core`` itself + ``claude_agent_sdk``.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest


# Python's built-in registry of stdlib top-level module names.
# Available since 3.10 — alice's runtime targets 3.11+ so this is
# safe.
_STDLIB = set(sys.stdlib_module_names)

ALLOWED_TOPLEVEL = _STDLIB | {
    "alice_core",
    "claude_agent_sdk",
}


CORE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "alice_core"


def _alice_core_python_files() -> list[pathlib.Path]:
    return sorted(
        p for p in CORE_DIR.rglob("*.py") if "__pycache__" not in p.parts
    )


def _toplevel_imports(tree: ast.AST) -> list[str]:
    """Pull the top-level module name from every Import / ImportFrom
    in ``tree``. Relative imports (``from . import x``) report
    nothing — they're internal-to-package and that's the point of
    the test."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import — stays inside alice_core. Skip.
                continue
            if node.module is None:
                continue
            names.append(node.module.split(".", 1)[0])
    return names


@pytest.mark.parametrize(
    "py_file",
    _alice_core_python_files(),
    ids=lambda p: p.relative_to(CORE_DIR.parent.parent).as_posix(),
)
def test_alice_core_imports_only_sdk_stdlib_or_self(py_file: pathlib.Path):
    """Every import in ``alice_core/`` must resolve to stdlib, the
    Claude Agent SDK, or another ``alice_core`` module. Sibling
    packages (``alice_speaking``, ``alice_thinking``,
    ``alice_viewer``, ``alice_watchers``, ``alice_prompts``,
    ``alice_indexer``, ``alice_skills``) are forbidden — the
    dependency direction is one-way."""
    tree = ast.parse(py_file.read_text())
    for name in _toplevel_imports(tree):
        assert name in ALLOWED_TOPLEVEL, (
            f"{py_file.relative_to(CORE_DIR.parent.parent)} imports "
            f"{name!r} — alice_core must depend only on stdlib, "
            f"claude_agent_sdk, or itself."
        )


def test_allow_list_contains_expected_anchors():
    """Pin a few names so a future Python that drops a stdlib
    module surfaces here rather than as a mysterious test failure
    on the parametrized rows."""
    for required in ("os", "json", "pathlib", "logging", "asyncio", "typing"):
        assert required in ALLOWED_TOPLEVEL, (
            f"stdlib {required!r} missing from allow-list — "
            f"sys.stdlib_module_names regression?"
        )
    assert "alice_core" in ALLOWED_TOPLEVEL
    assert "claude_agent_sdk" in ALLOWED_TOPLEVEL
