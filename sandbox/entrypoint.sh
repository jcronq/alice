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

# Install sidecars found under /home/alice/alice-tools/. Each tool owns its
# install.sh; we just invoke them. Failures are logged but don't abort
# container start — a broken sidecar shouldn't keep Alice from coming up.
if [ -d "$HOME/alice-tools" ]; then
    shopt -s nullglob
    for script in "$HOME"/alice-tools/*/install.sh; do
        tool="$(basename "$(dirname "$script")")"
        echo "[entrypoint] running $tool install.sh" >&2
        if ! bash "$script" >&2; then
            echo "[entrypoint] WARNING: $tool install.sh failed" >&2
        fi
    done
    shopt -u nullglob
fi

exec "$@"
