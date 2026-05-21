#!/usr/bin/env bash
# Alice container entrypoint. Runs once at container start, then execs CMD.
set -e

# Ensure the mount points exist even before volumes attach.
mkdir -p "$HOME/alice-mind" "$HOME/alice-tools" "$HOME/.config"

# Wrapper symlinks: /usr/local/bin/<name> -> /home/alice/alice/bin/<name>.
# The s6 services and other code reference these via the canonical
# /usr/local/bin/ path; the actual scripts live on the bind-mounted
# monorepo so edits + renames propagate to running containers without
# a docker rebuild. Issue #291: the alice_forge rename silently broke
# the sm-dispatcher in production because the baked-in wrapper still
# pointed at the retired `sm.dispatcher` module path. -f overwrites
# any stale baked-in copy left over from a pre-fix image.
for wrapper in alice-mind-autopush event-log alice-think \
               alice-thinker-watchdog alice-client \
               alice-gh-watcher alice-sm-dispatcher; do
    if [ -x "/home/alice/alice/bin/$wrapper" ]; then
        ln -sf "/home/alice/alice/bin/$wrapper" "/usr/local/bin/$wrapper"
    else
        echo "[entrypoint] WARNING: /home/alice/alice/bin/$wrapper missing or not executable" >&2
    fi
done

# Claude auth — resolved through the host's directory mounts so token
# refreshes on the host (via /login) become visible here without a
# container restart. See sandbox/docker-compose.yml for rationale.
if [ -d /host-claude ]; then
    mkdir -p "$HOME/.claude"
    ln -sf /host-claude/.credentials.json "$HOME/.claude/.credentials.json"
fi
# .claude.json: COPY from host (don't symlink). The host's claude binary
# rewrites this file constantly (it stores per-project session state and
# updates on every interaction); a live symlink lets Alice's claude
# subprocess hit a torn read mid-rewrite — JSON parse fails, the wake
# dies with "Unterminated string" / exit 1. With a copy, host edits no
# longer reach us, but Alice's claude has a stable file to read.
# Validate the read; retry briefly if the host was mid-write at the
# moment we copied; fall back to "{}" so claude has a parseable file
# even when validation never succeeds.
if [ -f /host-home/.claude.json ]; then
    python3 - <<'PY' >&2
import json, os, pathlib, shutil, time
src = pathlib.Path("/host-home/.claude.json")
dst = pathlib.Path(os.path.expanduser("~/.claude.json"))
ok = False
for attempt in range(5):
    try:
        data = src.read_text(encoding="utf-8")
        json.loads(data)
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(dst)
        ok = True
        break
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"[entrypoint] .claude.json copy attempt {attempt+1} failed: {exc}")
        time.sleep(0.2)
if not ok:
    print("[entrypoint] giving up on .claude.json copy; writing empty {}")
    dst.write_text("{}\n", encoding="utf-8")
PY
fi

# Codex → pi auth bridge. The host runs `codex login` (device-auth);
# the resulting ~/.codex/auth.json is mounted into /host-codex (read-
# only). Translate it into ~/alice/.pi/agent/auth.json so
# pi-coding-agent can use the ChatGPT subscription without its own
# browser-OAuth flow. Entrypoint runs as root; the alice user reads
# the file, so we chown after writing. Fail-soft: pi backends won't
# work, but Anthropic-side functionality still comes up.
if [ -d /host-codex ] && [ -x /home/alice/alice/bin/codex-to-pi-auth ]; then
    if /home/alice/alice/bin/codex-to-pi-auth \
            --codex /host-codex/auth.json \
            --pi /home/alice/.pi/agent/auth.json >&2; then
        chown -R alice:alice /home/alice/.pi
        echo "[entrypoint] pi auth bridged from /host-codex/auth.json" >&2
    else
        echo "[entrypoint] WARNING: codex-to-pi-auth failed; pi backend will not work" >&2
    fi
fi

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
