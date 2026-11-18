#!/usr/bin/env bash
# Boots the status-site server against a fixture ALICE_MIND, waits for
# it to come up, then hands off to ``npm run screenshot -- "$@"``. The
# server is killed on exit so the script is safe to re-run.
#
# Intended caller: the ``visual-regression`` CI job. Also convenient
# locally — ``./run-with-fixtures.sh --init`` captures baselines against
# the committed fixture dataset, no live alice needed.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
FIXTURES="$REPO/tests/status_site/fixtures/alice-mind"

if [[ ! -d "$FIXTURES/inner/status" ]]; then
  echo "run-with-fixtures: fixture dir missing: $FIXTURES" >&2
  exit 2
fi

PORT="${STATUS_SITE_PORT:-8795}"
KEY="${STATUS_SITE_KEY:-visualregression-$$}"
SCRATCH="$(mktemp -d -t status-fixture-XXXXXX)"
cp -r "$FIXTURES/." "$SCRATCH/"
KEY_FILE="$SCRATCH/key"
printf '%s' "$KEY" > "$KEY_FILE"

# Writable dirs the server tries to create/log into.
mkdir -p "$SCRATCH/uploads" "$SCRATCH/inner/surface" "$SCRATCH/inner/notes"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$SCRATCH" || true
}
trap cleanup EXIT

export PORT ALICE_MIND="$SCRATCH"
export STATUS_DIR="$SCRATCH/inner/status"
export SURFACE_DIR="$SCRATCH/inner/surface"
export UPLOAD_DIR="$SCRATCH/uploads"
export KEY_FILE
# Pollers hit the live vault; fixtures are static, so disable them.
export OVERVIEW_POLLERS=0
export ITEM_HISTORY=0
export ARCHIVE_ON_ROLL=0
# SSE still broadcasts, but on the one-shot fixture data the first
# snapshot is all we need.
export SSE_TICK_SECONDS=1
export SSE_HEARTBEAT_SECONDS=30

cd "$REPO"
# Prefer uv if available — the alice repo targets Python >=3.11 and uv
# resolves the venv automatically. Fall back to plain ``python3`` when
# uv is absent (e.g. a shell with PYTHONPATH already set).
if command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run --project "$REPO" python -m status_site)
else
  PYTHON_CMD=(python3 -m status_site)
fi
"${PYTHON_CMD[@]}" > "$SCRATCH/server.log" 2>&1 &
SERVER_PID=$!

# Wait up to 15s for the server to accept connections.
for i in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/index.html"; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "run-with-fixtures: server died during startup" >&2
    cat "$SCRATCH/server.log" >&2
    exit 1
  fi
  sleep 0.25
done
if ! curl -sf -o /dev/null "http://127.0.0.1:${PORT}/index.html"; then
  echo "run-with-fixtures: server never came up on :${PORT}" >&2
  cat "$SCRATCH/server.log" >&2
  exit 1
fi

export STATUS_SITE_URL="http://127.0.0.1:${PORT}"
export STATUS_SITE_KEY="$KEY"

cd "$HERE"
exec npm run --silent screenshot -- "$@"
