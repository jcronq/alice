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
    info "the subscription auth path uses it to mint a long-lived token."
    info "Skip if you'll use API-key auth instead. To install:"
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

oauth_in_env="${CLAUDE_CODE_OAUTH_TOKEN:-}"
api_base_in_env="${ANTHROPIC_BASE_URL:-}"
api_key_in_env="${ANTHROPIC_API_KEY:-}"

write_var_to_env() {
    # write_var_to_env KEY VALUE — upsert KEY=VALUE in alice.env (line-replace,
    # appending if absent). Used for the auth vars in either mode.
    local key="$1" value="$2"
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import sys, pathlib
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""
prefix = f"{key}="
out, replaced = [], False
for line in text.splitlines():
    if line.startswith(prefix):
        out.append(f"{prefix}{value}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"{prefix}{value}")
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
# read_text() does universal-newline translation (\r → \n) which would
# turn the PTY's \r\x1b[1B wrap into \n\x1b[1B, and after our cursor-down
# substitution that becomes \n\n — a false blank line that truncates
# the token at the wrap. Read bytes, decode manually.
data = pathlib.Path(sys.argv[1]).read_bytes().decode("utf-8", errors="replace")
# claude setup-token uses ANSI cursor-down (CSI N B) for visual line
# breaks instead of real newlines. Convert those to newlines first so
# our blank-line stop heuristic actually has something to match.
def _cud(m):
    n = int(m.group(1) or "1")
    return "\n" * n
data = re.sub(r"\x1b\[(\d*)B", _cud, data)
# Strip remaining ANSI / CSI sequences (color, cursor-right padding, etc).
data = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", data)
# CR is just column-zeroing within a line; drop it.
data = data.replace("\r", "")
i = data.rfind("sk-ant-oat01-")
if i < 0:
    sys.exit(1)
tail = data[i:]
m = re.search(r"\n[ \t]*\n", tail)                    # stop at blank line
if m:
    tail = tail[: m.start()]
candidate = re.sub(r"\s+", "", tail)                  # join wraps
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

if [ -n "$oauth_in_env" ]; then
    ok "Subscription auth already configured (CLAUDE_CODE_OAUTH_TOKEN in alice.env)"
elif [ -n "$api_base_in_env" ] || [ -n "$api_key_in_env" ]; then
    ok "API auth already configured (ANTHROPIC_* in alice.env)"
else
    cat <<EOF

    Alice's worker can authenticate to Claude two ways:

      [1] Claude subscription (default).
          A browser opens; sign in once; we capture a long-lived OAuth
          token (sk-ant-oat01-…) and write it to alice.env. Billing
          uses your Claude subscription. We do NOT use 'claude /login'
          — that token lives in the macOS Keychain (the container
          can't reach it) and refreshes don't propagate cleanly.

      [2] API key (direct or via LiteLLM proxy).
          Use a raw Anthropic API key, optionally routed through a
          LiteLLM proxy that maps to any backend. Billing follows
          whichever service the key/proxy points at.

EOF
    auth_mode=""
    while [ -z "$auth_mode" ]; do
        choice="$(ask "Choose [1] subscription or [2] api" "1")"
        case "$choice" in
            1|sub|subscription) auth_mode="subscription" ;;
            2|api|key)          auth_mode="api" ;;
            *)                  warn "Pick 1 or 2." ;;
        esac
    done

    if [ "$auth_mode" = "subscription" ]; then
        info "A browser will open. Sign in; we'll capture the token automatically."
        echo
        token=""
        if token=$(mint_token); then
            ok "Token captured: ${#token} chars, ends …${token: -8}"
            info "Compare to the token printed above — the last 8 chars should match."
        else
            info "Auto-capture didn't produce a token. Paste it manually:"
            token=$(ask "Token (sk-ant-oat01-…) or empty to abort")
            [ -z "$token" ] && fail "No token entered."
        fi
        write_var_to_env "CLAUDE_CODE_OAUTH_TOKEN" "$token"
        ok "Token written to $ENV_FILE"
        unset token
    else
        info "API mode. ANTHROPIC_BASE_URL is optional — leave blank to talk to"
        info "api.anthropic.com directly; set it to your LiteLLM (or other"
        info "Anthropic-compatible) proxy URL to route through there."
        echo
        api_base="$(ask "ANTHROPIC_BASE_URL (blank = direct anthropic)" "")"
        api_key=""
        while [ -z "$api_key" ]; do
            api_key="$(ask "ANTHROPIC_API_KEY (required)")"
            [ -z "$api_key" ] && warn "Required."
        done
        api_auth="$(ask "ANTHROPIC_AUTH_TOKEN (optional bearer for proxy)" "")"

        write_var_to_env "ANTHROPIC_BASE_URL" "$api_base"
        write_var_to_env "ANTHROPIC_API_KEY" "$api_key"
        write_var_to_env "ANTHROPIC_AUTH_TOKEN" "$api_auth"
        ok "API auth written to $ENV_FILE"
        [ -n "$api_base" ] && info "Endpoint: $api_base" || info "Endpoint: (default Anthropic API)"
        unset api_base api_key api_auth
    fi

    # The worker daemon caches the resolved auth into os.environ at
    # startup (daemon.run() → ensure_auth_env). If the container is
    # already running with a stale value, restart it so the fresh
    # config takes effect.
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx alice-worker-blue; then
        info "Restarting alice-worker-blue so it picks up the new auth…"
        docker restart alice-worker-blue >/dev/null
    fi
    # Re-source so the rest of the script sees the new vars.
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
    # Two diagnostic surfaces, in priority order:
    # - docker logs catches import-time crashes BEFORE the supervisor
    #   redirects stderr to a file (e.g. ModuleNotFoundError on a stale
    #   worker image). This is the *first* place to look on a fresh
    #   install failure.
    # - speaking.log is the structured app log; useful once the daemon
    #   has actually started but is misbehaving.
    info "First places to look:"
    info "    docker logs --tail 80 alice-worker-blue"
    info "    tail -F ~/.local/state/alice/worker/speaking.log"
    fail "Smoke test failed."
elif printf '%s' "$reply" | grep -qiE "error|authentication_failed"; then
    warn "Alice replied with an error response:"
    printf '%s\n' "$reply" | sed 's/^/        /'
    info ""
    info "Most likely: token in alice.env is invalid or expired."
    info "Logs:"
    info "    docker logs --tail 80 alice-worker-blue"
    info "    tail -F ~/.local/state/alice/worker/speaking.log"
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
