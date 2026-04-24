#!/usr/bin/env bash
# Run the alice-viewer on :7777 (binds 0.0.0.0 by default — LAN-reachable).
#
# Reads Alice's logs directly from their host bind-mount locations:
#   thinking  → ~/.local/state/alice/worker/thinking.log
#   speaking  → ~/.local/state/alice/worker/speaking.log
#   mind      → ~/alice-mind
# Override via env: ALICE_THINKING_LOG, ALICE_SPEAKING_LOG, ALICE_MIND.
# To restrict to loopback: ALICE_VIEWER_HOST=127.0.0.1 ./run.sh
set -eu

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "alice-viewer: uv not found on PATH" >&2
    exit 1
fi

# Sync deps into a uv-managed venv on first run.
uv sync >/dev/null

export ALICE_VIEWER_HOST="${ALICE_VIEWER_HOST:-0.0.0.0}"
export ALICE_VIEWER_PORT="${ALICE_VIEWER_PORT:-7777}"

echo "alice-viewer starting on http://$ALICE_VIEWER_HOST:$ALICE_VIEWER_PORT"
exec uv run alice-viewer
