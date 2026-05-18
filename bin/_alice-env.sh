# Shared compose-env setup for Alice's orchestration scripts.
#
# Source this file from any script that runs `docker compose` against
# sandbox/docker-compose.yml. Caller must set ALICE_ROOT to the repo
# root (typically: ALICE_ROOT="$(cd "$(dirname "$0")/.." && pwd)").
#
# Why this exists: alice-up and alice-deploy both need to export the
# same set of env vars (USER_ID, GROUP_ID, ALICE_REPO, ALICE_MIND,
# ALICE_TOOLS) so the compose file's bind mounts resolve to the same
# host paths. Inlining the exports in each script means a third
# orchestration entry point can silently disagree, and a deploy can
# remount the worker onto stub directories docker creates on first
# run — disconnected from the live state. (See issue #2.)
#
# Sourced files don't have their own shebang; intentional.

if [ -z "${ALICE_ROOT:-}" ]; then
    echo "_alice-env.sh: caller must set ALICE_ROOT before sourcing" >&2
    return 1 2>/dev/null || exit 1
fi

# Container's alice user matches the host invoker. Mounts inherit
# host permissions, so without this Alice (uid 1000) can't write to
# bind-mounted host directories.
export USER_ID="${USER_ID:-$(id -u)}"
export GROUP_ID="${GROUP_ID:-$(id -g)}"

# Bind the alice repo from wherever this script lives, so the worker's
# /home/alice/alice mount points at *this* checkout — no matter if it's
# ~/alice, ~/dev/alice, or somewhere else entirely.
export ALICE_REPO="${ALICE_REPO:-$ALICE_ROOT}"

# Mind + tools live under <repo>/data/ by default (gitignored as a whole).
# Mind is its own git repo; tools is just a directory of scripts.
# Override either env var to put them elsewhere.
export ALICE_MIND="${ALICE_MIND:-$ALICE_ROOT/data/alice-mind}"
export ALICE_TOOLS="${ALICE_TOOLS:-$ALICE_ROOT/data/alice-tools}"

# Source alice.env so any secrets the user keeps there (CLAUDE_*,
# ANTHROPIC_*, GH_TOKEN, etc.) are visible to compose's variable
# interpolation. The compose file only consumes the keys it explicitly
# references via ${VAR:-default}; unreferenced vars are inert. Done
# after the ALICE_* exports above so the file can override container
# paths if the operator chooses, without us caring how. Missing file
# is fine — install.sh hasn't run yet on a fresh checkout.
_alice_env_file="${ALICE_CONFIG:-$HOME/.config/alice/alice.env}"
if [ -f "$_alice_env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$_alice_env_file"
    set +a
fi
unset _alice_env_file

# Compose file stack. The base file always applies; the personal override
# (compose's auto-loaded second file) is included when present so we
# don't lose it by setting COMPOSE_FILE explicitly. docker-compose.gpu.yml
# stacks on top when the host has the nvidia runtime registered —
# embedder needs CUDA when /state/sci-env is installed, but most hosts
# reach the embedder over HTTP via ALICE_EMBED_URL and shouldn't pin
# `runtime: nvidia` (compose v2 rejects unknown runtimes hard, which
# breaks deploy on AMD or CPU-only boxes).
#
# Operator can short-circuit by exporting COMPOSE_FILE before invoking
# alice-up / alice-deploy.
if [ -z "${COMPOSE_FILE:-}" ]; then
    _compose_files="$ALICE_ROOT/sandbox/docker-compose.yml"
    if [ -f "$ALICE_ROOT/sandbox/docker-compose.override.yml" ]; then
        _compose_files="$_compose_files:$ALICE_ROOT/sandbox/docker-compose.override.yml"
    fi
    if command -v docker >/dev/null 2>&1 \
       && docker info --format '{{json .Runtimes}}' 2>/dev/null \
            | grep -q '"nvidia"'; then
        _compose_files="$_compose_files:$ALICE_ROOT/sandbox/docker-compose.gpu.yml"
    fi
    export COMPOSE_FILE="$_compose_files"
    unset _compose_files
fi
