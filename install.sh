#!/usr/bin/env bash
# Alice — interactive installer.
#
# Walks a fresh clone to a working CLI session in one pass. Idempotent:
# safe to re-run; skips steps that are already done. Read each section's
# header to know what's about to change on your machine.
#
# Usage:
#     ./install.sh

set -euo pipefail

ALICE_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HOME/.config/alice/alice.env"

# ---- output helpers --------------------------------------------------------

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'
else
    BOLD=""; DIM=""; RESET=""; GREEN=""; YELLOW=""; RED=""
fi

step()   { printf "\n%s==> %s%s\n" "$BOLD" "$*" "$RESET"; }
info()   { printf "    %s\n" "$*"; }
ok()     { printf "    %s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn()   { printf "    %s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
fail()   { printf "\n%s✗ %s%s\n" "$RED" "$*" "$RESET"; exit 1; }
ask()    {
    # ask PROMPT [DEFAULT]
    local reply default="${2:-}"
    if [ -n "$default" ]; then
        read -r -p "    $1 [$default]: " reply
    else
        read -r -p "    $1: " reply
    fi
    printf '%s' "${reply:-$default}"
}
confirm() {
    # confirm PROMPT [DEFAULT y|n]
    local default="${2:-y}"
    local reply
    read -r -p "    $1 [$default]: " reply
    reply="${reply:-$default}"
    case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---- 1. prerequisites ------------------------------------------------------

step "Checking prerequisites"

missing=()
for tool in docker git python3; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
    fail "Missing required tools: ${missing[*]}. Install them and re-run."
fi
ok "docker, git, python3 on PATH"

if ! docker info >/dev/null 2>&1; then
    fail "Docker daemon isn't reachable. Start Docker Desktop / Rancher Desktop and re-run."
fi
ok "Docker daemon reachable"

if ! command -v claude >/dev/null 2>&1; then
    info "Claude Code CLI not on PATH. Alice's worker doesn't need it, but"
    info "this installer uses it to mint a long-lived token. Install with:"
    info "    npm install -g @anthropic-ai/claude-code"
    confirm "Continue without it?" "n" || fail "Install claude and re-run."
fi
[ -n "$(command -v claude || true)" ] && ok "claude CLI on PATH"

# ---- 2. mind scaffold ------------------------------------------------------

step "Setting up Alice's mind"

if [ -f "$ENV_FILE" ]; then
    ok "$ENV_FILE already exists; skipping alice-init"
else
    info "Running alice-init (Signal off; CLI-only by default)..."
    "$ALICE_ROOT/bin/alice-init" --yes
fi

# Source whatever's in alice.env so we can branch on it. Don't fail if a
# blank line / comment confuses set -a; just read what we need.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE" 2>/dev/null || true
set +a

# ---- 3. Claude authentication ---------------------------------------------

step "Setting up Claude authentication"

token_in_env="${CLAUDE_CODE_OAUTH_TOKEN:-}"

write_token_to_env() {
    local token="$1"
    python3 - "$ENV_FILE" "$token" <<'PY'
import sys, pathlib
path, token = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""
out, replaced = [], False
for line in text.splitlines():
    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
        out.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")
p.write_text("\n".join(out).rstrip("\n") + "\n")
PY
    chmod 600 "$ENV_FILE"
}

mint_token() {
    # Print a freshly-minted long-lived OAuth token to stdout.
    # Returns non-zero if capture fails (caller falls back to manual paste).

    # 1. Prefer a non-interactive output flag if claude setup-token grows one.
    if claude setup-token --help 2>&1 | grep -qE -- '--print|--json|--output'; then
        local out
        out=$(claude setup-token --print 2>&1 || true)
        local tok
        tok=$(printf '%s' "$out" | grep -oE 'sk-ant-oat01-[A-Za-z0-9_-]+' | tail -1)
        if [ -n "$tok" ]; then
            printf '%s' "$tok"
            return 0
        fi
    fi

    # 2. Fall back to PTY capture: setup-token wants a real terminal
    #    (browser launch + interactive wait), so we can't just pipe stdout.
    local logfile
    logfile=$(mktemp -t alice-tok.XXXXXX) || return 1
    chmod 600 "$logfile"
    # shellcheck disable=SC2064
    trap "shred -u '$logfile' 2>/dev/null || rm -f '$logfile'" EXIT

    # Redirect script's display output to the terminal, not to mint_token's
    # stdout — otherwise command substitution would slurp the whole session
    # and the user would see nothing. `|| true` is critical: if claude
    # setup-token (or script itself) exits non-zero, set -e would otherwise
    # abort mint_token before the python extractor runs.
    printf '%s\n' "    Opening browser. Sign in, then come back here." >/dev/tty
    if [ "$(uname -s)" = "Darwin" ]; then
        script -q "$logfile" claude setup-token >/dev/tty 2>&1 || true
    else
        script -q -c "claude setup-token" "$logfile" >/dev/tty 2>&1 || true
    fi

    # Tokens can be longer than the terminal width; claude setup-token
    # emits a real \n at the wrap point. A naive grep stops there and
    # gives back half a token (silent corruption — Anthropic later
    # rejects it as "Invalid bearer token"). Read the raw stream, strip
    # ANSI, find the last sk-ant-oat01- occurrence, then walk forward
    # joining whitespace-separated continuations until a blank line.
    # set -e would abort mint_token if python exits non-zero, killing the
    # diagnostic block below. Force-tolerate failure here.
    local tok
    tok=$(python3 - "$logfile" <<'PY' || true
import re, sys, pathlib
data = pathlib.Path(sys.argv[1]).read_text(errors="replace")
data = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", data)   # strip ANSI/CSI
i = data.rfind("sk-ant-oat01-")
if i < 0:
    sys.exit(1)
tail = data[i:]
m = re.search(r"\n[ \t]*\n", tail)                    # stop at blank line
if m:
    tail = tail[: m.start()]
candidate = re.sub(r"[\s]+", "", tail)                # join wraps
m = re.match(r"sk-ant-oat01-[A-Za-z0-9_-]+", candidate)
if not m:
    sys.exit(1)
print(m.group(), end="")
PY
)

    local logsize=0
    [ -f "$logfile" ] && logsize=$(wc -c <"$logfile" | tr -d ' ')

    shred -u "$logfile" 2>/dev/null || rm -f "$logfile"
    trap - EXIT

    if [ -z "$tok" ]; then
        if [ "$logsize" = "0" ]; then
            printf '%s\n' "    auto-capture: script produced no output (browser flow may have been cancelled)" >/dev/tty
        else
            printf '%s\n' "    auto-capture: ${logsize}B captured but no sk-ant-oat01- token found" >/dev/tty
        fi
        return 1
    fi
    printf '%s' "$tok"
}

if [ -n "$token_in_env" ]; then
    ok "CLAUDE_CODE_OAUTH_TOKEN already set in alice.env"
else
    cat <<EOF

    Alice's worker authenticates to Claude with a long-lived OAuth token
    in alice.env. (We do NOT use 'claude /login' — on macOS that token
    lives in the system Keychain, which the container can't reach, and
    on any platform it's short-lived and refreshes don't propagate
    cleanly into the container.)

    A browser will open. Sign in; the token is captured automatically
    and written to alice.env.

EOF
    token=""
    if token=$(mint_token); then
        ok "Token captured: ${#token} chars, ends …${token: -8}"
        info "Compare to the token printed above — the last 8 chars should match."
    else
        info "Auto-capture didn't produce a token. Paste it manually:"
        token=$(ask "Token (sk-ant-oat01-…) or empty to abort")
        [ -z "$token" ] && fail "No token entered."
    fi

    write_token_to_env "$token"
    ok "Token written to $ENV_FILE"
    unset token

    # The worker daemon caches CLAUDE_CODE_OAUTH_TOKEN into os.environ at
    # startup (daemon.py:291). If the container is already running with a
    # stale token, restart it so the fresh value takes effect.
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx alice-worker-blue; then
        info "Restarting alice-worker-blue so it picks up the new token…"
        docker restart alice-worker-blue >/dev/null
    fi
    # Re-source so the rest of the script sees it.
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# ---- 4. optional Signal ----------------------------------------------------

step "Signal transport (optional)"

if [ -n "${SIGNAL_ACCOUNT:-}" ]; then
    ok "SIGNAL_ACCOUNT already configured ($SIGNAL_ACCOUNT)"
else
    info "Skip this if you only want CLI / Discord. You can add it later by"
    info "editing $ENV_FILE."
    if confirm "Configure Signal now?" "n"; then
        signal_account="$(ask "SIGNAL_ACCOUNT (E.164, e.g. +15555550100)")"
        allowed_senders="$(ask "ALLOWED_SENDERS (e.g. +15555551212:Alice)")"
        python3 - "$ENV_FILE" "$signal_account" "$allowed_senders" <<'PY'
import sys, pathlib
path, account, senders = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
text = p.read_text()
out = []
for line in text.splitlines():
    if line.startswith("SIGNAL_ACCOUNT="):
        out.append(f"SIGNAL_ACCOUNT={account}")
    elif line.startswith("ALLOWED_SENDERS="):
        out.append(f"ALLOWED_SENDERS={senders}")
    else:
        out.append(line)
p.write_text("\n".join(out).rstrip("\n") + "\n")
PY
        ok "Signal config written. Register the account after alice-up:"
        info "    docker exec -it alice-daemon signal-cli -a '$signal_account' link -n 'Alice'"
    fi
fi

# ---- 5. bring up the sandbox ----------------------------------------------

step "Bringing up Alice's sandbox"

info "First run builds three Docker images (~3-8 min depending on cache)."
"$ALICE_ROOT/bin/alice-up"
ok "Containers up"

# ---- 6. smoke test ---------------------------------------------------------

step "Smoke test"

info "Asking Alice for a one-line hello (90s cap)…"
set +e
reply="$("$ALICE_ROOT/bin/alice" -p "Reply with exactly: hello from alice" --timeout 90 2>&1)"
rc=$?
set -e

if [ $rc -ne 0 ]; then
    warn "alice -p exited with code $rc."
    info "Output:"
    printf '%s\n' "$reply" | sed 's/^/        /'
    info ""
    if printf '%s' "$reply" | grep -qiE "auth"; then
        info "Looks like an authentication error. The token in alice.env may be"
        info "invalid or the worker hasn't picked it up yet. Try:"
        info "    docker restart alice-worker-blue"
        info "    alice -p 'ping'"
    fi
    info "Speaking-daemon stderr is at:"
    info "    ~/.local/state/alice/worker/speaking-stderr.log"
    fail "Smoke test failed."
elif printf '%s' "$reply" | grep -qiE "error|authentication_failed"; then
    warn "Alice replied with an error response:"
    printf '%s\n' "$reply" | sed 's/^/        /'
    info ""
    info "Most likely: token in alice.env is invalid or expired."
    info "Logs: ~/.local/state/alice/worker/speaking-stderr.log"
    fail "Smoke test failed."
else
    ok "Alice replied:"
    printf '%s\n' "$reply" | sed 's/^/        /'
fi

# ---- 7. wrap up ------------------------------------------------------------

step "Install complete"

cat <<EOF

    Talk to Alice:
        $ALICE_ROOT/bin/alice                  # interactive
        $ALICE_ROOT/bin/alice -p "what's up"   # one-shot

    Add the wrappers to your PATH (persist in your shell rc):
        export PATH="$ALICE_ROOT/bin:\$PATH"

    Watch logs:
        tail -F ~/.local/state/alice/worker/speaking-stderr.log
        tail -F ~/.local/state/alice/worker/speaking.log
        tail -F ~/.local/state/alice/worker/thinking.log

    Viewer (read-only introspection UI):
        http://localhost:7777

    Tear down:
        $ALICE_ROOT/bin/alice-down              # stop, keep state
        $ALICE_ROOT/bin/alice-down --rm         # stop and remove containers

EOF
