#!/usr/bin/env bash
# Install .githooks/ as the repo's hook directory.
#
# Run once per clone:
#     ./scripts/install-hooks.sh
#
# After this, git invokes hooks from .githooks/ (tracked in-tree) instead of
# .git/hooks/ (per-clone, untracked). Idempotent — safe to re-run.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config core.hooksPath .githooks

# Ensure the hooks are executable (git won't fix mode for us on Windows / fresh clones).
chmod +x .githooks/* 2>/dev/null || true

echo "install-hooks: core.hooksPath -> .githooks (active hooks: $(ls .githooks | tr '\n' ' '))"
