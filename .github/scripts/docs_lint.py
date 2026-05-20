#!/usr/bin/env python3
"""Docs-update lint for PRs.

Compares the PR head against the base branch. If source-code files changed
but no ``docs/**`` file changed and the PR does not carry the
``docs:not-applicable`` label, emit a warning naming the source files and
the closest ``docs/`` stubs.

Initial mode: warn-only (exits 0). To escalate to a hard block once docs
coverage matures, flip ``MODE`` below to ``"fail"``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Flip to "fail" to make this a hard block. Keep "warn" until docs coverage
# matures — see docs/README.md for the policy.
MODE: str = "warn"

BYPASS_LABEL: str = "docs:not-applicable"

# File extensions considered "source code" for the purposes of this lint.
SOURCE_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
)

# Path fragments that exclude a file from being treated as source code,
# even if its suffix matches above.
EXCLUDE_FRAGMENTS: tuple[str, ...] = (
    "/tests/",
    "/test/",
    "/__pycache__/",
    "/eval_runs/",
    "/.github/",
    "/sandbox/",
    "/scripts/",
)


def run(cmd: list[str]) -> str:
    """Run ``cmd`` and return stdout, raising on non-zero exit."""
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def changed_files(base_sha: str, head_sha: str) -> list[str]:
    """Return the list of files changed between ``base_sha`` and ``head_sha``."""
    out = run(["git", "diff", "--name-only", f"{base_sha}...{head_sha}"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def is_source(path: str) -> bool:
    """True if ``path`` looks like source code we'd expect docs for."""
    if not path.endswith(SOURCE_SUFFIXES):
        return False
    normalized = "/" + path
    return not any(frag in normalized for frag in EXCLUDE_FRAGMENTS)


def is_doc(path: str) -> bool:
    """True if ``path`` is under ``docs/``."""
    return path.startswith("docs/")


def has_bypass_label() -> bool:
    """True if the PR carries the bypass label.

    Reads from the GitHub event payload at ``$GITHUB_EVENT_PATH``. Falls
    back to False if the payload is unavailable (e.g. local invocation).
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).is_file():
        return False
    try:
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    labels = payload.get("pull_request", {}).get("labels", []) or []
    return any(lbl.get("name") == BYPASS_LABEL for lbl in labels)


def closest_doc_stub(source_path: str) -> str:
    """Best-effort hint at which docs file the author should consider.

    For ``src/alice_<pkg>/...`` returns ``docs/components/alice_<pkg>.md``
    if such a stub exists, else ``docs/components/README.md``. For
    ``backend/cozyhem/automations/...`` returns
    ``docs/components/automations.md`` if it exists. Otherwise
    ``docs/README.md``.
    """
    parts = source_path.split("/")
    if len(parts) >= 2 and parts[0] == "src" and parts[1].startswith("alice_"):
        candidate = Path("docs/components") / f"{parts[1]}.md"
        if candidate.is_file():
            return str(candidate)
        return "docs/components/README.md"
    if len(parts) >= 3 and parts[0] == "backend" and parts[1] == "cozyhem":
        candidate = Path("docs/components") / f"{parts[2]}.md"
        if candidate.is_file():
            return str(candidate)
        return "docs/components/README.md"
    return "docs/README.md"


def main() -> int:
    base_sha = os.environ.get("BASE_SHA")
    head_sha = os.environ.get("HEAD_SHA")
    if not base_sha or not head_sha:
        print("docs_lint: BASE_SHA / HEAD_SHA not set; skipping.", file=sys.stderr)
        return 0

    try:
        files = changed_files(base_sha, head_sha)
    except subprocess.CalledProcessError as exc:
        print(f"docs_lint: git diff failed: {exc}", file=sys.stderr)
        return 0

    source_changed = [f for f in files if is_source(f)]
    docs_changed = [f for f in files if is_doc(f)]

    if not source_changed:
        print("docs_lint: no source-code changes detected; nothing to check.")
        return 0

    if docs_changed:
        print(
            "docs_lint: docs/** updated alongside source changes — "
            f"{len(docs_changed)} file(s)."
        )
        return 0

    if has_bypass_label():
        print(
            f"docs_lint: bypass label '{BYPASS_LABEL}' applied; "
            "skipping docs check."
        )
        return 0

    # No docs change, no bypass label, and source did change. Emit a clear
    # warning. The closest-stub hint is best-effort.
    print("=" * 72)
    print("docs_lint WARNING: source code changed without a docs/** update.")
    print("=" * 72)
    print()
    print(
        "Every PR should update the relevant documentation. See "
        "docs/README.md for the policy."
    )
    print()
    print("Source files changed (first 20):")
    for path in source_changed[:20]:
        stub = closest_doc_stub(path)
        print(f"  - {path}  ->  consider updating {stub}")
    if len(source_changed) > 20:
        print(f"  ... and {len(source_changed) - 20} more.")
    print()
    print(
        f"If this PR has no docs impact, apply the '{BYPASS_LABEL}' label "
        "and rerun."
    )
    print()

    if MODE == "fail":
        return 1
    print("docs_lint: warn mode — not failing the build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
