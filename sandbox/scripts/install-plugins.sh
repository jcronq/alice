#!/usr/bin/env bash
# install-plugins.sh — read config/plugins.yaml, pip-install each
# declared plugin into /opt/alice-venv, then copy its s6 service
# directories from `<site-packages>/<top_module>/s6/<service>/` into
# `/etc/s6-overlay/s6-rc.d/<service>/` and register each service under
# `user/contents.d/<service>` so the s6-rc bundle starts it.
#
# Invoked as a RUN step in sandbox/Dockerfile after the native s6
# services have been copied. The Dockerfile owns the `chmod +x` sweep
# over run/up/finish files (one find call covers natives + plugins).
#
# Convention: each plugin's wheel ships its s6 service tree as package
# data at `<top_module>/s6/<service>/`. The script discovers the dir by
# globbing under site-packages — the plugin author does not declare
# `top_module` separately. `<top_module>` is usually the package name
# with hyphens converted to underscores (alice-cozylobe →
# alice_cozylobe), but the search is convention-free: any directory
# named `<service>` two levels deep under site-packages, on a path that
# ends in `/s6/<service>`, is accepted.
#
# Exits non-zero on the first failure (set -euo pipefail). A missing
# service dir for a declared service is a build error, not a warning —
# silent skips would let a typo in the manifest produce an image that
# looks fine but never starts the plugin.

set -euo pipefail

MANIFEST="${1:-/tmp/plugins.yaml}"
VENV="${ALICE_VENV:-/opt/alice-venv}"
S6_RC_D="${S6_RC_D:-/etc/s6-overlay/s6-rc.d}"

if [ ! -f "$MANIFEST" ]; then
    echo "install-plugins: manifest not found at $MANIFEST" >&2
    exit 1
fi

# site.getsitepackages() can return multiple paths on some interpreter
# layouts (notably system-site-packages venvs). Iterate so the search
# covers all of them — the first one only happens to be the wheel
# install target on the alice container today, but that's not a
# guarantee any plugin author should have to know about.
mapfile -t SITE_PACKAGES_DIRS < <("$VENV/bin/python" -c 'import site
for p in site.getsitepackages():
    print(p)')

# Parse the manifest with the alice venv's pyyaml (already installed in
# the base stage; no new dep). Emit one pipe-delimited line per plugin
# so the bash side can iterate without sourcing a Python tempfile.
mapfile -t plugin_lines < <("$VENV/bin/python" - "$MANIFEST" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as fh:
    cfg = yaml.safe_load(fh) or {}

plugins = cfg.get("plugins") or []
for plugin in plugins:
    name = plugin["name"]
    source = plugin["source"]
    services = ",".join(plugin.get("services") or [])
    print(f"{name}|{source}|{services}")
PY
)

if [ "${#plugin_lines[@]}" -eq 0 ]; then
    echo "install-plugins: manifest has no plugins; nothing to install."
    exit 0
fi

mkdir -p "$S6_RC_D/user/contents.d"

for line in "${plugin_lines[@]}"; do
    IFS='|' read -r name source services <<<"$line"

    echo "==> Installing plugin: $name  (source: $source)"
    "$VENV/bin/pip" install --no-cache-dir "$source"

    IFS=',' read -ra svc_list <<<"$services"
    for svc in "${svc_list[@]}"; do
        [ -z "$svc" ] && continue

        # Refuse to clobber a service dir that already exists — that
        # would only happen if a native s6 dir at sandbox/s6/<svc>/ was
        # copied earlier in the Dockerfile and now collides with a
        # plugin shipping the same service name. Surface the conflict
        # rather than silently doing the wrong thing.
        if [ -e "$S6_RC_D/$svc" ]; then
            echo "install-plugins: service dir '$S6_RC_D/$svc' already" \
                 "exists — plugin '$name' would clobber it. Rename one" \
                 "side or remove the native dir." >&2
            exit 1
        fi

        # Locate the service dir inside site-packages. The expected
        # layout is `<site-packages>/<top_module>/s6/<svc>/`. -path
        # filters on the `/s6/` segment so we don't match an unrelated
        # directory that happens to share the service name. Search each
        # site-packages dir until a match is found.
        match=""
        for sp in "${SITE_PACKAGES_DIRS[@]}"; do
            [ -d "$sp" ] || continue
            match=$(find "$sp" -mindepth 3 -maxdepth 4 -type d \
                        -name "$svc" -path "*/s6/$svc" -print -quit)
            [ -n "$match" ] && break
        done
        if [ -z "$match" ]; then
            echo "install-plugins: plugin '$name' declared service '$svc'" \
                 "but no matching dir found under any site-packages" \
                 "(${SITE_PACKAGES_DIRS[*]}). Expected <pkg>/s6/$svc/." >&2
            exit 1
        fi

        echo "    $svc  <-  $match"
        # cp -a preserves the +x bit on run scripts the wheel built with.
        # The Dockerfile's post-install find+chmod pass is belt-and-
        # suspenders for plugins built without preserving exec bits.
        cp -a "$match" "$S6_RC_D/$svc"

        # s6-rc bundle membership: an empty file under user/contents.d
        # named for the service is all s6 needs to autostart it.
        : > "$S6_RC_D/user/contents.d/$svc"
    done
done

echo "install-plugins: done."
