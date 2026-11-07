"""Tests for :mod:`alice_state_db.components` — file-path → bucket mapping.

The status-site Overview pipeline (issue #821) groups active work by
which top-level ``src/`` directory a PR touches. The helper functions
here are the single source of truth for that mapping; the poller,
renderer, and renderer tests all route through them.
"""

from __future__ import annotations

import pytest

from alice_state_db import components as C


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/alice_forge/dispatcher/main.py", "alice_forge"),
        ("src/alice_speaking/daemon.py", "alice_speaking"),
        ("src/alice_state_db/schema.py", "alice_state_db"),
        ("src/status_site/sources/pipeline.py", "status_site"),
        ("src/core/agent_library/agents.py", "core"),
        ("src/watchers/gh_watcher.py", "watchers"),
        # single-segment src/ file still counts as the component
        ("src/__init__.py", "__init__.py"),
    ],
)
def test_component_for_path_src_first_segment(path, expected):
    assert C.component_for_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "sandbox/docker-compose.yml",
        "sandbox/Dockerfile",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "Dockerfile.prod",
    ],
)
def test_component_for_path_infra_bucket(path):
    assert C.component_for_path(path) == C.INFRA


@pytest.mark.parametrize(
    "path",
    [
        "docs/design/789.md",
        "README.md",
        "pyproject.toml",
        "scripts/deploy.sh",
    ],
)
def test_component_for_path_misc_bucket(path):
    assert C.component_for_path(path) == C.MISC


@pytest.mark.parametrize("path", ["", "   ", "\t", None, 42])
def test_component_for_path_rejects_empty_or_non_string(path):
    assert C.component_for_path(path) is None


def test_component_for_path_normalises_leading_dot_slash():
    assert C.component_for_path("./src/alice_forge/x.py") == "alice_forge"
    assert C.component_for_path("/src/alice_forge/x.py") == "alice_forge"


def test_components_for_paths_dedups_and_sorts():
    paths = [
        "src/alice_forge/a.py",
        "src/alice_forge/b.py",
        "src/status_site/sources/pipeline.py",
        "sandbox/Dockerfile",
        "README.md",
        "",
        "docs/789.md",
    ]
    result = C.components_for_paths(paths)
    assert result == ["alice_forge", "infra", "misc", "status_site"]


def test_components_for_paths_empty_input_returns_empty():
    assert C.components_for_paths([]) == []


def test_components_for_paths_only_blanks_returns_empty():
    assert C.components_for_paths(["", "  ", None]) == []  # type: ignore[list-item]
