#!/bin/bash
# Signal → Claude Code bridge.
#
# Reads inbound messages from signal-cli's daemon log, runs them through
# `claude` with a per-sender session, and sends the reply back via signal-cli's
# JSON-RPC endpoint.
#
# Blue/green aware:
# - Acquires an exclusive lease via flock before reading anything — only one
#   bridge ever processes messages at a time. A second bridge starting up
#   blocks on the lease until the first exits.
# - Tracks byte offset into the daemon log so restarts don't miss or replay
#   messages across bridge handoffs.
# - Dedups on envelope timestamp as a safety net against log duplication.
# - Drains gracefully on SIGTERM: waits for in-flight claude calls to finish,
#   persists state, releases lease, exits cleanly.

set -euo pipefail

# -------- traps + cleanup --------
DRAINING=0
TAIL_PID=""

cleanup() {
    local ec=$?
    trap - EXIT
    # Kill tail + any remaining subprocesses.
    [ -n "$TAIL_PID" ] && kill -TERM "$TAIL_PID" 2>/dev/null || true
    jobs -p | xargs -r kill 2>/dev/null || true
    pkill -P $$ 2>/dev/null || true
    # Lease fd 9 closes on exit — kernel releases the flock automatically,
    # which is the signal that "we're gone". No sentinel file to clean up;
    # probes use `flock -n` to test the lock directly.
    exit "$ec"
}
trap cleanup EXIT INT TERM

on_sigterm() {
    DRAINING=1
    log "SIGTERM received; entering drain mode"
    # Poke the tail so the main read loop wakes up and notices DRAINING.
    [ -n "$TAIL_PID" ] && kill -TERM "$TAIL_PID" 2>/dev/null || true
}

# -------- config --------
ALICE_CONFIG="${ALICE_CONFIG:-$HOME/.config/alice/alice.env}"
if [[ -r "$ALICE_CONFIG" ]]; then
    # shellcheck disable=SC1090
    source "$ALICE_CONFIG"
else
    echo "ERROR: alice.env not found at $ALICE_CONFIG" >&2
    exit 2
fi

: "${SIGNAL_API:?SIGNAL_API must be set in alice.env}"
: "${SIGNAL_ACCOUNT:?SIGNAL_ACCOUNT must be set in alice.env}"
: "${ALLOWED_SENDERS:?ALLOWED_SENDERS must be set in alice.env}"
: "${WORK_DIR:?WORK_DIR must be set in alice.env}"
CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-240}"
FLOCK_TIMEOUT="${FLOCK_TIMEOUT:-300}"
CLAUDE_ALLOWED_TOOLS="${CLAUDE_ALLOWED_TOOLS:-Bash,Read,Write,Edit,Glob,Grep}"
SIGNAL_LOG_FILE="${SIGNAL_LOG_FILE:-/state/daemon/signal-daemon.log}"

# Shared state dir across blue/green workers.
STATE_DIR="${STATE_DIR:-/state/worker}"
LOG_FILE="$STATE_DIR/signal-bridge.log"
SESSION_DIR="$STATE_DIR/signal-sessions"
LOCK_DIR="$STATE_DIR/locks"
LEASE_FILE="$STATE_DIR/lease"
OFFSET_FILE="$STATE_DIR/offset"
SEEN_FILE="$STATE_DIR/seen-timestamps"
SEEN_MAX=1000

mkdir -p "$STATE_DIR" "$SESSION_DIR" "$LOCK_DIR"

# -------- allowed-senders parse --------
declare -A NAMES=()
IFS=',' read -ra _pairs <<< "$ALLOWED_SENDERS"
for _pair in "${_pairs[@]}"; do
    _num="${_pair%%:*}"
    _name="${_pair#*:}"
    [[ -n "$_num" && -n "$_name" ]] && NAMES["$_num"]="$_name"
done
unset _pairs _pair _num _name

# -------- log helper --------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# -------- lease --------
# fd 9 held for the lifetime of this process; closing = releasing.
acquire_lease() {
    log "waiting for lease at $LEASE_FILE"
    touch "$LEASE_FILE"
    exec 9>"$LEASE_FILE"
    flock 9
    log "lease acquired (pid $$)"
    # Liveness is the flock itself — external probes check via
    # `flock -n $LEASE_FILE true` (succeeds iff no one holds the lock).
}

# -------- offset --------
get_offset() {
    if [[ -f "$OFFSET_FILE" ]]; then
        cat "$OFFSET_FILE"
    else
        echo 0
    fi
}

save_offset() {
    local off="$1"
    local tmp="$OFFSET_FILE.tmp.$$"
    echo "$off" > "$tmp"
    mv -f "$tmp" "$OFFSET_FILE"
}

# -------- dedup --------
declare -A SEEN_TS=()
SEEN_TS_ORDER=()

load_seen() {
    if [[ -f "$SEEN_FILE" ]]; then
        while IFS= read -r ts; do
            [[ -n "$ts" ]] || continue
            SEEN_TS[$ts]=1
            SEEN_TS_ORDER+=("$ts")
        done < "$SEEN_FILE"
        if [[ ${#SEEN_TS_ORDER[@]} -gt $SEEN_MAX ]]; then
            local trimmed=( "${SEEN_TS_ORDER[@]:$(( ${#SEEN_TS_ORDER[@]} - SEEN_MAX ))}" )
            SEEN_TS_ORDER=( "${trimmed[@]}" )
            SEEN_TS=()
            for ts in "${SEEN_TS_ORDER[@]}"; do SEEN_TS[$ts]=1; done
            printf '%s\n' "${SEEN_TS_ORDER[@]}" > "$SEEN_FILE.tmp.$$"
            mv -f "$SEEN_FILE.tmp.$$" "$SEEN_FILE"
        fi
    fi
}

is_duplicate() {
    local ts="$1"
    [[ -n "${SEEN_TS[$ts]:-}" ]]
}

mark_seen() {
    local ts="$1"
    [[ -z "$ts" ]] && return
    SEEN_TS[$ts]=1
    SEEN_TS_ORDER+=("$ts")
    printf '%s\n' "$ts" >> "$SEEN_FILE"
    # Periodic compaction.
    if [[ ${#SEEN_TS_ORDER[@]} -gt $((SEEN_MAX * 2)) ]]; then
        local trimmed=( "${SEEN_TS_ORDER[@]:$(( ${#SEEN_TS_ORDER[@]} - SEEN_MAX ))}" )
        SEEN_TS_ORDER=( "${trimmed[@]}" )
        SEEN_TS=()
        for t in "${SEEN_TS_ORDER[@]}"; do SEEN_TS[$t]=1; done
        printf '%s\n' "${SEEN_TS_ORDER[@]}" > "$SEEN_FILE.tmp.$$"
        mv -f "$SEEN_FILE.tmp.$$" "$SEEN_FILE"
    fi
}

# -------- Signal send --------
send_signal() {
    local recipient="$1"
    local message="$2"

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
        if [[ $total -gt 1 ]]; then
            payload="($i/$total) $chunk"
        fi
        local request
        request=$(jq -cn \
            --arg account "$SIGNAL_ACCOUNT" \
            --arg msg "$payload" \
            --arg recipient "$recipient" \
            '{jsonrpc:"2.0",method:"send",id:"send",params:{account:$account,message:$msg,recipients:[$recipient]}}')
        curl -sS -X POST "$SIGNAL_API/api/v1/rpc" \
            -H "Content-Type: application/json" \
            -d "$request" \
            >> "$LOG_FILE" 2>&1
    done
}

send_typing() {
    local recipient="$1"
    local request
    request=$(jq -cn \
        --arg account "$SIGNAL_ACCOUNT" \
        --arg recipient "$recipient" \
        '{jsonrpc:"2.0",method:"sendTyping",id:"typing",params:{account:$account,recipients:[$recipient]}}')
    curl -sS -X POST "$SIGNAL_API/api/v1/rpc" \
        -H "Content-Type: application/json" \
        -d "$request" > /dev/null 2>&1 || true
}

# -------- session helper --------
get_session_file() {
    local sender="$1"
    local safe="${sender//+/}"
    echo "$SESSION_DIR/${safe}.session"
}

ensure_session_is_valid() {
    local session_file="$1"
    [[ -f "$session_file" ]] || return 0
    local sid
    sid=$(cat "$session_file" 2>/dev/null || true)
    [[ -z "$sid" ]] && { rm -f "$session_file"; return 0; }
    local projects_root="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
    local project_slug
    project_slug=$(echo "$WORK_DIR" | tr '/' '-' | sed 's/^-//')
    if [[ ! -f "${projects_root}/-${project_slug}/${sid}.jsonl" ]]; then
        log "Stale session $sid — clearing"
        rm -f "$session_file"
    fi
}

# -------- message processing --------
process_message() {
    local sender="$1"
    local body="$2"
    local attachments="$3"   # newline-separated "path|contentType|filename" triples
    local name="${NAMES[$sender]:-$sender}"
    local session_file
    session_file=$(get_session_file "$sender")
    local lock_file="$LOCK_DIR/${sender//+/}.lock"

    (
        # Send immediately so the user sees typing right away, then every
        # 10s to keep Signal's 15s typing-indicator TTL refreshed.
        send_typing "$sender"
        while true; do
            sleep 10
            send_typing "$sender"
        done
    ) </dev/null >/dev/null 2>&1 &
    local typing_pid=$!

    # Status update loop: first ping after 45s, then every 60s, for long jobs
    local status_interval="${STATUS_UPDATE_INTERVAL:-60}"
    (
        sleep 45
        local elapsed=45
        while true; do
            send_signal "$sender" "Still working... (${elapsed}s)"
            sleep "$status_interval"
            elapsed=$(( elapsed + status_interval ))
        done
    ) </dev/null >/dev/null 2>&1 &
    local status_pid=$!

    trap 'kill "$typing_pid" "$status_pid" 2>/dev/null || true' RETURN

    (
        flock -w "$FLOCK_TIMEOUT" 9 || {
            log "ERROR: lock timeout ($FLOCK_TIMEOUT s) for $name — notifying sender"
            send_signal "$sender" "Still working on your previous message — hold on."
            return
        }

        log "Processing message from $name: ${body:0:100}..."

        ensure_session_is_valid "$session_file"

        local now
        now=$(TZ="${TZ:-America/New_York}" date '+%A, %B %-d, %Y at %-I:%M %p %Z')

        local prompt="[Signal from ${name} | ${now}]

${body}"

        # Append attachment file references so Claude can Read them
        if [[ -n "$attachments" ]]; then
            while IFS='|' read -r att_path att_ct att_fname; do
                [[ -z "$att_path" ]] && continue
                prompt+=$'\n'"[Attachment: $att_path ($att_ct, \"$att_fname\") — use the Read tool to view]"
                log "Attachment in prompt: $att_path"
            done <<< "$attachments"
        fi

        local -a claude_args=(-p "$prompt" --allowedTools "$CLAUDE_ALLOWED_TOOLS")
        if [[ -f "$session_file" ]]; then
            local sid
            sid=$(cat "$session_file")
            claude_args+=(--resume "$sid")
            log "Resuming session $sid"
        fi

        local raw_output exit_code stderr_file raw_stderr
        local claude_cmd="${CLAUDE_CMD:-claude}"
        stderr_file=$(mktemp)
        if [[ "${CLAUDE_TIMEOUT:-0}" -gt 0 ]]; then
            raw_output=$(cd "$WORK_DIR" && timeout --preserve-status "$CLAUDE_TIMEOUT" "$claude_cmd" "${claude_args[@]}" --output-format json < /dev/null 2> "$stderr_file") && exit_code=0 || exit_code=$?
        else
            raw_output=$(cd "$WORK_DIR" && "$claude_cmd" "${claude_args[@]}" --output-format json < /dev/null 2> "$stderr_file") && exit_code=0 || exit_code=$?
        fi
        raw_stderr=$(cat "$stderr_file")
        [[ -s "$stderr_file" ]] && cat "$stderr_file" >> "$LOG_FILE"
        rm -f "$stderr_file"

        if [[ $exit_code -ne 0 ]]; then
            log "ERROR: claude exit $exit_code for $name"
            local kind="claude-error"
            [[ $exit_code -eq 143 || $exit_code -eq 124 ]] && kind="claude-timeout"
            event-log error system component=signal-bridge kind="$kind" \
                detail="claude exit $exit_code for $name" --source signal-bridge >> "$LOG_FILE" 2>&1 || true
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

# -------- envelope routing --------
handle_envelope_line() {
    local line="$1"
    [[ "$line" == "{"* ]] || return 0

    local sender body ts
    sender=$(echo "$line" | jq -r '.envelope.source // .envelope.sourceNumber // empty' 2>/dev/null) || return 0
    body=$(echo "$line" | jq -r '.envelope.dataMessage.message // empty' 2>/dev/null) || return 0
    ts=$(echo "$line" | jq -r '.envelope.timestamp // empty' 2>/dev/null) || true

    # Build newline-separated "path|contentType|filename" list for each attachment
    local attachments=""
    attachments=$(echo "$line" | jq -r '
        .envelope.dataMessage.attachments // [] |
        .[] |
        ["/host-home/.local/share/signal-cli/attachments/" + .id,
         (.contentType // "application/octet-stream"),
         (.filename // .id)] |
        join("|")
    ' 2>/dev/null) || true

    [[ -z "$sender" ]] && return 0
    # Skip if no text and no attachments
    [[ -z "$body" && -z "$attachments" ]] && return 0

    if [[ -z "${NAMES[$sender]+x}" ]]; then
        log "Ignoring message from unknown sender: $sender"
        return 0
    fi

    if [[ -n "$ts" ]] && is_duplicate "$ts"; then
        log "skipping duplicate envelope ts=$ts"
        return 0
    fi
    [[ -n "$ts" ]] && mark_seen "$ts"

    process_message "$sender" "$body" "$attachments" &
}

# -------- wait for daemon --------
wait_for_daemon() {
    local i
    for i in $(seq 1 60); do
        if curl -sfo /dev/null --connect-timeout 3 --max-time 5 \
            -X POST "$SIGNAL_API/api/v1/rpc" \
            -H 'Content-Type: application/json' \
            -d '{"jsonrpc":"2.0","method":"version","id":"ping"}' 2>/dev/null; then
            log "Daemon ready."
            return 0
        fi
        sleep 1
    done
    log "ERROR: daemon not reachable at $SIGNAL_API after 60s"
    return 1
}

# -------- catchup --------
catchup() {
    local start_off
    start_off=$(get_offset)
    local size
    size=$(stat -c %s "$SIGNAL_LOG_FILE" 2>/dev/null || echo 0)

    if [[ $start_off -gt $size ]]; then
        log "catchup: saved offset $start_off > log size $size; log was truncated. resetting to 0"
        start_off=0
        save_offset 0
    fi
    if [[ $start_off -eq $size ]]; then
        log "catchup: no backlog"
        return
    fi

    log "catchup: reading from offset $start_off (current size $size)"
    local cur=$start_off
    local count=0
    while IFS= read -r line; do
        local bytes=${#line}
        cur=$(( cur + bytes + 1 ))
        save_offset "$cur"
        handle_envelope_line "$line" || true
        count=$((count + 1))
    done < <(tail -c "+$((start_off + 1))" "$SIGNAL_LOG_FILE" 2>/dev/null)
    log "catchup: processed $count lines up to offset $cur"
}

# -------- main loop --------
main_loop() {
    local start_off
    start_off=$(get_offset)
    log "main: tailing $SIGNAL_LOG_FILE from offset $start_off"

    trap on_sigterm TERM

    # tail -F --follow=name handles signal-daemon restart (log file rotate).
    exec 3< <(exec tail -F --follow=name -c "+$((start_off + 1))" "$SIGNAL_LOG_FILE" 2>/dev/null)
    # Capture the tail's PID so on_sigterm can kill it to break the read loop.
    TAIL_PID=$(pgrep -P $$ -f 'tail -F --follow=name' | head -n1 || true)

    local cur=$start_off
    while IFS= read -r line <&3; do
        if [[ $DRAINING -eq 1 ]]; then
            log "main: drain flag set, exiting read loop"
            break
        fi
        local bytes=${#line}
        cur=$(( cur + bytes + 1 ))
        save_offset "$cur"
        handle_envelope_line "$line" || true
    done

    exec 3<&-
    log "main: read loop exited"
    log "drain: waiting for in-flight message handlers"
    wait || true
    log "drain: complete"
}

# -------- entry --------
log "=== Signal bridge starting (pid $$) ==="
acquire_lease
load_seen
wait_for_daemon || exit 1
catchup
log "Listening for messages..."
main_loop
log "=== Signal bridge exiting ==="
