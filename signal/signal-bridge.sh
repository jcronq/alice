#!/bin/bash
# Signal → Claude Code bridge.
# Reads inbound messages from signal-cli's daemon journalctl stream,
# runs them through `claude` with a per-sender session, and sends the reply back.

set -euo pipefail

# Kill every subprocess we spawn (tail, background sends, etc.) on exit or
# TERM/INT, so a supervisor restart doesn't leave orphan tails double-reading
# the signal log and processing each message twice.
cleanup() {
    local ec=$?
    trap - EXIT
    jobs -p | xargs -r kill 2>/dev/null || true
    # Also kill any descendants not tracked by jobs (grandchildren, the tail
    # inside the `tail | while read` pipeline).
    pkill -P $$ 2>/dev/null || true
    exit "$ec"
}
trap cleanup EXIT INT TERM

ALICE_CONFIG="${ALICE_CONFIG:-$HOME/.config/alice/alice.env}"
if [[ -r "$ALICE_CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$ALICE_CONFIG"
else
    echo "ERROR: alice.env not found at $ALICE_CONFIG" >&2
    exit 2
fi

# Validate required config
: "${SIGNAL_API:?SIGNAL_API must be set in alice.env}"
: "${SIGNAL_ACCOUNT:?SIGNAL_ACCOUNT must be set in alice.env}"
: "${ALLOWED_SENDERS:?ALLOWED_SENDERS must be set in alice.env}"
: "${WORK_DIR:?WORK_DIR must be set in alice.env}"
CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-240}"
FLOCK_TIMEOUT="${FLOCK_TIMEOUT:-300}"
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-Bash,Read,Write,Edit,Glob,Grep}"

LOG_FILE="${HOME}/.local/state/alice/signal-bridge.log"
SESSION_DIR="${HOME}/.local/state/alice/signal-sessions"
LOCK_DIR="/tmp/signal-bridge-locks"

mkdir -p "$(dirname "$LOG_FILE")" "$SESSION_DIR" "$LOCK_DIR"

# Parse ALLOWED_SENDERS (format: "+15555550100:Name,+15555550101:Other") into NAMES map
declare -A NAMES=()
IFS=',' read -ra _pairs <<< "$ALLOWED_SENDERS"
for _pair in "${_pairs[@]}"; do
    _num="${_pair%%:*}"
    _name="${_pair#*:}"
    [[ -n "$_num" && -n "$_name" ]] && NAMES["$_num"]="$_name"
done
unset _pairs _pair _num _name

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# Send raw message via signal-cli daemon. Chunks at paragraph boundaries under 4000 chars.
send_signal() {
    local recipient="$1"
    local message="$2"

    # Simple chunking: if the message fits, send whole; otherwise split on paragraph
    # boundaries under 4000 chars per chunk.
    local chunks=()
    if [[ ${#message} -le 4000 ]]; then
        chunks=("$message")
    else
        local remaining="$message"
        while [[ -n "$remaining" ]]; do
            if [[ ${#remaining} -le 4000 ]]; then
                chunks+=("$remaining")
                break
            fi
            # Try to split on a paragraph break near 4000; fall back to hard cut
            local head="${remaining:0:4000}"
            local cut=${#head}
            local nl="${head##*$'\n\n'}"
            if [[ ${#nl} -lt ${#head} ]]; then
                cut=$(( ${#head} - ${#nl} ))
            fi
            chunks+=("${remaining:0:cut}")
            remaining="${remaining:cut}"
        done
    fi

    local total=${#chunks[@]}
    local i=0
    for chunk in "${chunks[@]}"; do
        i=$((i + 1))
        local payload="$chunk"
        # Prefix with (i/N) for multi-part replies so the reader knows to wait
        if [[ $total -gt 1 ]]; then
            payload="($i/$total) $chunk"
        fi
        curl -sf -X POST "$SIGNAL_API/api/v1/rpc" \
            -H "Content-Type: application/json" \
            -d "$(jq -n --arg msg "$payload" --arg to "$recipient" '{
                jsonrpc: "2.0", method: "send", id: "send",
                params: { message: $msg, recipients: [$to] }
            }')" >> "$LOG_FILE" 2>&1 || true
        [[ $i -lt $total ]] && sleep 1
    done
}

send_typing() {
    local recipient="$1"
    curl -sf -X POST "$SIGNAL_API/api/v1/rpc" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg to "$recipient" '{
            jsonrpc: "2.0", method: "sendTyping", id: "typing",
            params: { recipient: $to }
        }')" >/dev/null 2>&1 || true
}

get_session_file() {
    local sender="$1"
    local safe="${sender//+/}"
    echo "$SESSION_DIR/${safe}.session"
}

# Validate that the stored session ID still exists in Claude Code's project store.
# If not, delete the session file so the next turn starts fresh instead of erroring forever.
ensure_session_is_valid() {
    local session_file="$1"
    [[ -f "$session_file" ]] || return 0
    local sid
    sid=$(cat "$session_file" 2>/dev/null || true)
    [[ -z "$sid" ]] && { rm -f "$session_file"; return 0; }
    # Claude Code's per-project session transcripts live at <projects_root>/<project>/<sid>.jsonl.
    # When alice runs sandboxed, her sessions persist in ~/.alice-claude/projects/... on the host.
    local projects_root="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
    local project_slug
    project_slug=$(echo "$WORK_DIR" | tr '/' '-' | sed 's/^-//')
    if [[ ! -f "${projects_root}/-${project_slug}/${sid}.jsonl" ]]; then
        log "Stale session $sid — clearing"
        rm -f "$session_file"
    fi
}

process_message() {
    local sender="$1"
    local body="$2"
    local name="${NAMES[$sender]:-$sender}"
    local session_file
    session_file=$(get_session_file "$sender")
    local lock_file="$LOCK_DIR/${sender//+/}.lock"

    # Send typing indicator immediately (before lock) — user sees "..." right away
    # even if the flock is held by a previous message.
    send_typing "$sender"

    # Keep the typing indicator alive with a heartbeat (Signal expires it after ~15s)
    (
        while true; do
            sleep 10
            send_typing "$sender"
        done
    ) </dev/null >/dev/null 2>&1 &
    local typing_pid=$!
    trap 'kill "$typing_pid" 2>/dev/null || true' RETURN

    # Serialize messages per sender
    (
        flock -w "$FLOCK_TIMEOUT" 9 || {
            log "ERROR: lock timeout ($FLOCK_TIMEOUT s) for $name — notifying sender"
            send_signal "$sender" "Still working on your previous message — hold on."
            return
        }

        log "Processing message from $name: ${body:0:100}..."

        ensure_session_is_valid "$session_file"

        local now
        now=$(TZ=America/New_York date '+%A, %B %-d, %Y at %-I:%M %p %Z')

        local prompt="[Signal from ${name} | ${now}]

${body}"

        local -a claude_args=(-p "$prompt" --allowedTools "$CLAUDE_ALLOWED_TOOLS")
        if [[ -f "$session_file" ]]; then
            local sid
            sid=$(cat "$session_file")
            claude_args+=(--resume "$sid")
            log "Resuming session $sid"
        fi

        # Bound claude with a timeout so the lock always releases before FLOCK_TIMEOUT.
        # Capture stderr to a temp file so we can relay the actual error on failure.
        # CLAUDE_CMD defaults to `claude` (on-host) but can be set to `alice` in
        # alice.env to run claude inside the sandbox container.
        # CD into WORK_DIR so claude's project slug matches the session file
        # (claude keys sessions by CWD hash).
        local raw_output exit_code stderr_file raw_stderr
        local claude_cmd="${CLAUDE_CMD:-claude}"
        stderr_file=$(mktemp)
        raw_output=$(cd "$WORK_DIR" && timeout --preserve-status "$CLAUDE_TIMEOUT" "$claude_cmd" "${claude_args[@]}" --output-format json < /dev/null 2> "$stderr_file") && exit_code=0 || exit_code=$?
        raw_stderr=$(cat "$stderr_file")
        [[ -s "$stderr_file" ]] && cat "$stderr_file" >> "$LOG_FILE"
        rm -f "$stderr_file"

        if [[ $exit_code -ne 0 ]]; then
            # Note: with --preserve-status, `timeout` propagates the signal-induced
            # exit (e.g. 143 = 128 + SIGTERM from the kill) rather than returning 124.
            log "ERROR: claude exit $exit_code for $name"
            # Session file intentionally preserved so the next turn can --resume.
            local err_msg="Hit an error (exit $exit_code). Session preserved — reply to retry."
            if [[ -n "$raw_stderr" ]]; then
                local stderr_preview="${raw_stderr: -1500}"
                err_msg+=$'\n\nstderr:\n'"$stderr_preview"
            fi
            send_signal "$sender" "$err_msg"
            return
        fi

        local response session_id_out
        response=$(echo "$raw_output" | jq -r '.result // empty' 2>/dev/null)
        session_id_out=$(echo "$raw_output" | jq -r '.session_id // empty' 2>/dev/null)

        if [[ -n "$session_id_out" ]]; then
            echo "$session_id_out" > "$session_file"
            log "Session: $session_id_out"
        fi

        if [[ -z "$response" ]]; then
            log "WARNING: empty response from claude for $name"
            return
        fi

        log "Sending response to $name (${#response} chars)"
        send_signal "$sender" "$response"
    ) 9>"$lock_file"
}

wait_for_daemon() {
    local i
    for i in $(seq 1 30); do
        if curl -sf "$SIGNAL_API/api/v1/rpc" -X POST \
            -H "Content-Type: application/json" \
            -d '{"jsonrpc":"2.0","method":"listAccounts","id":"ping"}' >/dev/null 2>&1; then
            log "Daemon ready."
            return 0
        fi
        sleep 2
    done
    log "ERROR: daemon not reachable after 60s"
    return 1
}

handle_envelope_line() {
    local line="$1"
    # Skip non-JSON lines (INFO/WARN from signal-cli)
    [[ "$line" == "{"* ]] || return 0

    local sender body
    sender=$(echo "$line" | jq -r '.envelope.source // .envelope.sourceNumber // empty' 2>/dev/null) || return 0
    body=$(echo "$line" | jq -r '.envelope.dataMessage.message // empty' 2>/dev/null) || return 0

    [[ -z "$sender" || -z "$body" ]] && return 0

    if [[ -z "${NAMES[$sender]+x}" ]]; then
        log "Ignoring message from unknown sender: $sender"
        return 0
    fi

    process_message "$sender" "$body" &
}

log "=== Signal bridge starting ==="

wait_for_daemon || exit 1

log "Listening for messages..."

# Supervising loop: if the tail exits (pipe break, log rotation, daemon
# restart), we restart the tail rather than silently exiting.
#
# SIGNAL_LOG_FILE overrides the journalctl-based source — used when the
# bridge runs in a container and signal-daemon's output is redirected to a
# file instead of systemd's journal.
while true; do
    if [ -n "${SIGNAL_LOG_FILE:-}" ]; then
        tail -F -n0 "$SIGNAL_LOG_FILE" 2>/dev/null
    else
        journalctl -u signal-daemon -f -o cat --no-pager 2>/dev/null
    fi | while IFS= read -r line; do
        handle_envelope_line "$line"
    done || true
    log "signal tail exited — restarting in 2s"
    sleep 2
done
