"""Tests for :mod:`status_site.sources.components` (issue #821)."""

from __future__ import annotations

import pytest

from status_site.sources.components import (
    CANONICAL_BUCKET_ORDER,
    component_for_path,
    order_buckets,
)


# ─── component_for_path ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/alice_forge/dispatcher/gh.py", "alice_forge"),
        ("src/alice_speaking/daemon.py", "alice_speaking"),
        ("src/alice_thinking/wake.py", "alice_thinking"),
        ("src/alice_state_db/gh_poll.py", "alice_state_db"),
        ("src/alice_bug_fix_dispatcher/foo.py", "alice_bug_fix_dispatcher"),
        ("src/alice_pricing/calc.py", "alice_pricing"),
        ("src/status_site/server.py", "status_site"),
        ("src/watchers/gh.py", "watchers"),
        ("src/core/config.py", "core"),
        ("src/eval/scorer.py", "eval"),
        ("src/indexer/foo.py", "indexer"),  # not in canonical order; still derived
    ],
)
def test_component_for_path_matches_each_canonical_src_dir(path, expected):
    assert component_for_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "sandbox/docker-compose.yml",
        "sandbox/Dockerfile.alice",
        "s6/alice-speaking/run",
        "s6/alice-gh-watcher/run",
        "Dockerfile.alice",
        "Dockerfile",
    ],
)
def test_component_for_path_detects_infra_paths(path):
    assert component_for_path(path) == "infra"


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "pyproject.toml",
        "tests/status_site/test_x.py",
        "tests/test_pipeline.py",
        "docs/ARCHITECTURE.md",
        "docs/designs/foo.md",
        ".pre-commit-config.yaml",
        "",
    ],
)
def test_component_for_path_returns_none_for_unmatched(path):
    assert component_for_path(path) is None


def test_component_for_path_does_not_match_src_file_at_root():
    # ``src/README.md`` is a root-under-src file, not a component dir
    # entry. Our regex requires ``src/<name>/<something>``.
    assert component_for_path("src/README.md") is None


# ─── order_buckets ────────────────────────────────────────────────────


def test_order_buckets_respects_canonical_order():
    # Pass in canonical names scrambled; expect canonical back.
    canonical_without_prebuild = [b for b in CANONICAL_BUCKET_ORDER if b != "pre-build"]
    scrambled = list(reversed(canonical_without_prebuild))
    assert order_buckets(scrambled) == canonical_without_prebuild


def test_order_buckets_prebuild_always_last():
    out = order_buckets({"pre-build", "alice_forge", "status_site"})
    assert out == ["alice_forge", "status_site", "pre-build"]


def test_order_buckets_unknown_sort_before_prebuild_alphabetically():
    out = order_buckets({"alice_forge", "indexer", "metrics", "pre-build"})
    # alice_forge (canonical) first, then unknowns alphabetical, then pre-build last.
    assert out == ["alice_forge", "indexer", "metrics", "pre-build"]


def test_order_buckets_drops_missing_canonical():
    out = order_buckets({"status_site", "core"})
    # No alice_forge in input → not in output.
    assert out == ["status_site", "core"]
