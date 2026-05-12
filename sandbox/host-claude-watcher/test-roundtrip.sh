#!/usr/bin/env bash
# test-roundtrip.sh — smoke test for the host-claude-watcher daemon.
#
# We can't actually invoke `claude` from CI (no API key, no host
# binary), so we stub it out with a fake claude binary that just echoes
# its stdin. The daemon-script logic — parse frontmatter, run binary,
# write outbox, archive inbox — is what we're verifying here.
#
# Usage:
#   ./test-roundtrip.sh          # uses fake claude under tmp
#
# Exits 0 on success, nonzero on any check failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCHER="$SCRIPT_DIR/alice-host-claude-watcher.sh"

if [[ ! -x "$WATCHER" ]]; then
    echo "FAIL: watcher not executable at $WATCHER" >&2
    exit 1
fi

WORK=$(mktemp -d)
trap "rm -rf '$WORK'" EXIT

# Stub claude: echo "FAKE-CLAUDE:" + stdin to stdout. Real claude would
# call the API; this keeps the test offline and deterministic.
mkdir -p "$WORK/bin"
cat >"$WORK/bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "FAKE-CLAUDE-OK"
cat
EOF
chmod +x "$WORK/bin/claude"

# Synthesize an inbox file. UTC timestamp matches the on-the-wire shape
# the MCP tool produces.
ROOT="$WORK/host-claude"
mkdir -p "$ROOT/inbox" "$ROOT/outbox" "$ROOT/.handled"

ID="2026-05-12T00-00-00Z-roundtrip-smoke"
cat >"$ROOT/inbox/$ID.md" <<EOF
---
id: $ID
requested_by: speaking
created_at: 2026-05-12T00:00:00Z
urgency: normal
timeout_seconds: 30
allow_destructive: false
---
# Task

echo hello
EOF

# Run the daemon in single-shot mode.
ALICE_HOST_CLAUDE_ROOT="$ROOT" \
ALICE_HOST_CLAUDE_BIN="$WORK/bin/claude" \
    "$WATCHER" --single-shot

# Verify outbox file exists.
OUT="$ROOT/outbox/$ID.md"
if [[ ! -f "$OUT" ]]; then
    echo "FAIL: no outbox file at $OUT" >&2
    ls -la "$ROOT/outbox" >&2 || true
    exit 1
fi

# Verify it contains the expected stdout content.
if ! grep -q 'FAKE-CLAUDE-OK' "$OUT"; then
    echo "FAIL: outbox missing FAKE-CLAUDE-OK marker" >&2
    cat "$OUT" >&2
    exit 1
fi
if ! grep -q '^status: success' "$OUT"; then
    echo "FAIL: outbox status != success" >&2
    cat "$OUT" >&2
    exit 1
fi
if ! grep -q "id: $ID" "$OUT"; then
    echo "FAIL: outbox missing id frontmatter" >&2
    cat "$OUT" >&2
    exit 1
fi

# Verify inbox file was moved to .handled/<today>/.
if [[ -f "$ROOT/inbox/$ID.md" ]]; then
    echo "FAIL: inbox file still present (should have been archived)" >&2
    exit 1
fi
TODAY=$(date -u +'%Y-%m-%d')
if [[ ! -f "$ROOT/.handled/$TODAY/$ID.md" ]]; then
    echo "FAIL: inbox file not in .handled/$TODAY/" >&2
    ls -la "$ROOT/.handled" >&2 || true
    exit 1
fi

echo "OK: round-trip succeeded"
echo "    inbox  -> .handled/$TODAY/$ID.md"
echo "    outbox -> outbox/$ID.md (status=success)"
