#!/usr/bin/env bash
# Alice container entrypoint. Runs once at container start, then execs CMD.
set -e

# Ensure the mount points exist even before volumes attach.
mkdir -p "$HOME/alice-mind" "$HOME/alice-tools" "$HOME/.config"

# Point git at gh for HTTPS auth. The mounted ~/.config/gh provides the token.
if command -v gh >/dev/null 2>&1; then
    git config --global credential."https://github.com".helper '!gh auth git-credential' 2>/dev/null || true
    git config --global credential."https://gist.github.com".helper '!gh auth git-credential' 2>/dev/null || true
fi

exec "$@"
