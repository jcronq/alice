#!/usr/bin/env bash
# alice-host-claude-watcher.sh
#
# Host-side daemon that bridges Speaking (running inside the alice-worker
# container) to a Claude CLI running on the host.
#
# Flow:
#   1. Speaking drops a markdown task spec into INBOX_DIR with frontmatter.
#   2. This daemon notices it (inotify or polling fallback), parses the
#      frontmatter, runs `claude --print --max-turns 50` against the body,
#      and writes the captured stdout/stderr to OUTBOX_DIR/<id>.md.
#   3. The original inbox file moves to HANDLED_DIR/YYYY-MM-DD/<id>.md so
#      we have a record and we don't reprocess it.
#
# This script ships with the repo (sandbox/host-claude-watcher/) so it's
# version-controlled. Deploy is a manual step: copy to /usr/local/bin/ and
# enable the companion systemd unit. See README.md.
#
# Flags:
#   --single-shot   Drain the inbox once, then exit. Used by the smoke
#                   test and by manual one-shot runs. No watch loop.
#
# Environment overrides (rare):
#   ALICE_HOST_CLAUDE_ROOT   Base dir (default: /state/worker/host-claude)
#   ALICE_HOST_CLAUDE_BIN    Path to claude binary (default: looked up in PATH)
#
# Logging: stderr only, suitable for systemd journal capture.

set -euo pipefail

ROOT="${ALICE_HOST_CLAUDE_ROOT:-/state/worker/host-claude}"
INBOX_DIR="$ROOT/inbox"
OUTBOX_DIR="$ROOT/outbox"
HANDLED_DIR="$ROOT/.handled"

# Truncation cap matches the tool-side contract documented in the issue.
MAX_OUTPUT_BYTES=51200  # 50 KB

SINGLE_SHOT=0
for arg in "$@"; do
    case "$arg" in
        --single-shot) SINGLE_SHOT=1 ;;
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

log() {
    # ISO-8601 UTC, level, message → stderr (journal-friendly).
    printf '%s [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$1" "$2" >&2
}

# Startup banner. The sha256 prefix is a deploy-freshness check: after a
# repo change to this script, `journalctl -u alice-host-claude.service`
# should show a different `script_sha=` than before. Pre-#144 the
# systemd unit ran a stale /usr/local/bin/ copy and there was no way to
# tell from the journal whether the new code was loaded.
SCRIPT_SHA="$(sha256sum "$0" 2>/dev/null | awk '{print substr($1,1,12)}')"
log INFO "alice-host-claude-watcher starting script_path=$0 script_sha=${SCRIPT_SHA:-unknown} inbox_dir=$INBOX_DIR"

# Look up the claude binary up-front. Refuse to run without it; a daemon
# that silently no-ops every inbox file is worse than one that doesn't
# start.
CLAUDE_BIN="${ALICE_HOST_CLAUDE_BIN:-$(command -v claude || true)}"
if [[ -z "$CLAUDE_BIN" || ! -x "$CLAUDE_BIN" ]]; then
    log ERROR "claude binary not found in PATH (set ALICE_HOST_CLAUDE_BIN to override)"
    exit 1
fi
log INFO "claude binary: $CLAUDE_BIN"

mkdir -p "$INBOX_DIR" "$OUTBOX_DIR" "$HANDLED_DIR"

# parse_frontmatter <file>
#
# Reads a markdown file that starts with a YAML-ish frontmatter block:
#
#   ---
#   key: value
#   ---
#   # body...
#
# Emits two halves to two files:
#   $FRONTMATTER_TMP  — the frontmatter key:value lines (no fences)
#   $BODY_TMP         — everything after the closing ---
#
# We use python3 (always available on a modern host) because awk-based
# parsing of even modest YAML is fragile and these specs are user-authored.
parse_frontmatter() {
    local src="$1"
    python3 - "$src" "$FRONTMATTER_TMP" "$BODY_TMP" <<'PYEOF'
import pathlib
import sys

src = pathlib.Path(sys.argv[1]).read_text()
fm_out = pathlib.Path(sys.argv[2])
body_out = pathlib.Path(sys.argv[3])

lines = src.splitlines(keepends=True)
if not lines or lines[0].rstrip() != "---":
    # No frontmatter — whole file is body.
    fm_out.write_text("")
    body_out.write_text(src)
    sys.exit(0)

fm_lines = []
i = 1
while i < len(lines) and lines[i].rstrip() != "---":
    fm_lines.append(lines[i])
    i += 1
# i now points at the closing --- (or EOF if malformed).
body = "".join(lines[i + 1:]) if i < len(lines) else ""
fm_out.write_text("".join(fm_lines))
body_out.write_text(body)
PYEOF
}

# Read a single frontmatter value (defaulting if missing).
fm_get() {
    local key="$1"
    local default="$2"
    local val
    val=$(awk -F': *' -v k="$key" '$1==k { sub(/^[^:]*: */, ""); print; exit }' "$FRONTMATTER_TMP")
    if [[ -z "$val" ]]; then
        printf '%s' "$default"
    else
        # Strip surrounding quotes if present.
        val="${val%\"}"; val="${val#\"}"
        val="${val%\'}"; val="${val#\'}"
        printf '%s' "$val"
    fi
}

# Truncate a file in place to MAX_OUTPUT_BYTES with a tail marker.
truncate_with_marker() {
    local f="$1"
    local size
    size=$(wc -c <"$f")
    if (( size > MAX_OUTPUT_BYTES )); then
        head -c "$MAX_OUTPUT_BYTES" "$f" >"$f.trunc"
        printf '\n\n[...truncated %d bytes; original was %d bytes...]\n' \
            "$(( size - MAX_OUTPUT_BYTES ))" "$size" >>"$f.trunc"
        mv "$f.trunc" "$f"
    fi
}

process_file() {
    local inbox_path="$1"
    local fname
    fname=$(basename "$inbox_path")
    local id="${fname%.md}"

    log INFO "processing $fname"

    # Per-task temp scratch.
    local workdir
    workdir=$(mktemp -d)
    # shellcheck disable=SC2064
    trap "rm -rf '$workdir'" RETURN

    FRONTMATTER_TMP="$workdir/fm"
    BODY_TMP="$workdir/body"
    local stdout_tmp="$workdir/stdout"
    local stderr_tmp="$workdir/stderr"

    if ! parse_frontmatter "$inbox_path"; then
        log ERROR "frontmatter parse failed for $fname; skipping (left in inbox)"
        return 1
    fi

    local timeout_seconds
    timeout_seconds=$(fm_get timeout_seconds 600)
    # Defensive: coerce to integer; fall back to 600 on garbage.
    if ! [[ "$timeout_seconds" =~ ^[0-9]+$ ]]; then
        log WARN "invalid timeout_seconds=$timeout_seconds for $fname; using 600"
        timeout_seconds=600
    fi

    local started_at
    started_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Run claude with the body on stdin. timeout(1) handles wall-clock cap;
    # we capture exit codes specially so 124 (timeout's signal) maps to
    # status=timeout in the outbox.
    set +e
    timeout --preserve-status --signal=TERM --kill-after=10 \
        "$timeout_seconds" \
        "$CLAUDE_BIN" --print --max-turns 50 \
        <"$BODY_TMP" \
        >"$stdout_tmp" 2>"$stderr_tmp"
    local exit_code=$?
    set -e

    local finished_at
    finished_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    local status
    if (( exit_code == 124 || exit_code == 137 )); then
        status="timeout"
    elif (( exit_code == 0 )); then
        status="success"
    else
        status="failure"
    fi

    truncate_with_marker "$stdout_tmp"
    truncate_with_marker "$stderr_tmp"

    # Write outbox file atomically — staging + rename — so a partial-write
    # crash never leaves the speaking-side poller looking at a half-file.
    local outbox_path="$OUTBOX_DIR/$id.md"
    local outbox_tmp="$OUTBOX_DIR/.$id.md.tmp"
    {
        printf -- '---\n'
        printf 'id: %s\n' "$id"
        printf 'status: %s\n' "$status"
        printf 'started_at: %s\n' "$started_at"
        printf 'finished_at: %s\n' "$finished_at"
        printf 'exit_code: %d\n' "$exit_code"
        printf -- '---\n'
        printf '# Stdout\n\n'
        cat "$stdout_tmp"
        printf '\n# Stderr\n\n'
        cat "$stderr_tmp"
        printf '\n'
    } >"$outbox_tmp"
    mv "$outbox_tmp" "$outbox_path"

    # Move inbox file into the dated handled bucket.
    local today
    today=$(date -u +'%Y-%m-%d')
    local handled_subdir="$HANDLED_DIR/$today"
    mkdir -p "$handled_subdir"
    mv "$inbox_path" "$handled_subdir/$fname"

    log INFO "done $fname status=$status exit=$exit_code"
}

drain_inbox() {
    # shopt nullglob isn't on by default; guard with a glob-existence test.
    local found=0
    for f in "$INBOX_DIR"/*.md; do
        [[ -e "$f" ]] || continue
        found=1
        # Don't let one bad file kill the daemon.
        if ! process_file "$f"; then
            log ERROR "process_file failed for $f"
        fi
    done
    if (( found == 0 )) && (( SINGLE_SHOT == 1 )); then
        log INFO "inbox empty"
    fi
}

if (( SINGLE_SHOT == 1 )); then
    drain_inbox
    exit 0
fi

# Watch loop. Prefer inotifywait; fall back to a 5s polling loop.
if command -v inotifywait >/dev/null 2>&1; then
    log INFO "watching $INBOX_DIR with inotifywait"
    # Drain anything sitting in the inbox before we start listening — files
    # that arrived while the daemon was down would otherwise wait for the
    # next new event.
    drain_inbox
    while true; do
        # -m is load-bearing. The MCP tool writes via tempfile+rename in
        # the same dir, which fires CLOSE_WRITE on the .tmp file before
        # the MOVED_TO on the final .md name. Without -m, inotifywait
        # exits on the first event — the CLOSE_WRITE for `.foo.md.tmp`
        # — which we filter out as non-`*.md`, and the subsequent
        # MOVED_TO `foo.md` is gone by the time the outer loop respawns
        # inotifywait. Result: every request stalls until the next
        # unrelated event happens to wake the watcher.
        inotifywait -m -q -e close_write,moved_to --format '%f' "$INBOX_DIR" \
            | while read -r created; do
                case "$created" in
                    *.md) ;;
                    *) continue ;;
                esac
                # The MCP tool writes via tempfile+rename so the file is
                # complete by the time we see it. Still, a tiny sleep
                # smooths over any rename-vs-event race.
                sleep 0.05
                if [[ -e "$INBOX_DIR/$created" ]]; then
                    process_file "$INBOX_DIR/$created" || \
                        log ERROR "process_file failed for $created"
                fi
            done
        # inotifywait exited unexpectedly (kernel-watch exhaustion, signal,
        # etc). Drain whatever's queued before restarting so files that
        # arrived during the gap aren't stranded until the next event.
        log WARN "inotifywait exited; draining inbox before restart"
        drain_inbox
    done
else
    log WARN "inotifywait not available; falling back to 5s polling"
    while true; do
        drain_inbox
        sleep 5
    done
fi
