FROM debian:trixie-slim

# Single-container fold of sandbox/worker/Dockerfile + sandbox/daemon/Dockerfile
# + sandbox/viewer/Dockerfile (issue #152 / EKS Phase 2 §1). The three legacy
# Dockerfiles stay in place until the docker-compose / alice-deploy cutover
# lands; this image is additive scaffolding for now.
#
# Layer order is least-churning → most-churning so warm rebuilds touch only the
# top layers: base → OS deps → signal-cli → node globals → python venv → s6
# overlay → user/dirs → app scripts → s6 tree → env.

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG SIGNAL_CLI_VERSION=0.14.1
ARG S6_OVERLAY_VERSION=3.2.2.0
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV NODE_MAJOR=24

# OS apt deps — union of worker + daemon + viewer needs. Worker is the
# superset on non-Java packages; daemon contributes openjdk-25 on arm64 only
# (added inline in the signal-cli layer below to keep this layer arch-agnostic).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git openssh-client jq less xz-utils \
        ripgrep fd-find tini procps util-linux python3 python3-venv python3-pip \
        inotify-tools \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

# signal-cli — arch-branched, folded from sandbox/daemon/Dockerfile:21-36.
# amd64 uses the GraalVM native build (no JVM in the image). arm64 has no
# native build upstream, so install OpenJDK 25 from trixie main and use the
# JRE-based tarball. (signal-cli 0.14.1 targets JavaVersion.VERSION_25; lower
# JDKs hit UnsupportedClassVersionError at load.)
RUN apt-get update \
    && case "${TARGETARCH}" in \
         amd64) \
             curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
                 | tar -xz -C /usr/local/bin \
             && chmod +x /usr/local/bin/signal-cli \
             ;; \
         arm64) \
             apt-get install -y --no-install-recommends openjdk-25-jre-headless \
             && curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
                 | tar -xz -C /opt \
             && ln -s /opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli /usr/local/bin/signal-cli \
             ;; \
         *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest

# pi-coding-agent — drives the PiKernel (Codex / OpenAI subscription
# routing) when model.yml routes a hemisphere through `backend: pi`.
# Pinned: spike report (docs/refactor/09-spike-pi-coding-agent.md)
# verified the JSONL event vocabulary against 0.71.0; later versions
# need a re-check before bumping.
RUN npm install -g @mariozechner/pi-coding-agent@0.71.0

# Single venv with the union of worker + viewer pip deps. Worker contributes
# the agent kernel + speaking/thinking deps; viewer contributes fastapi /
# uvicorn / sse-starlette. claude-agent-sdk + httpx + jinja2 are shared and
# listed once. ~30 MB extra over the worker-only image — accepted, single
# venv. Debian marks system pip as externally-managed; a venv isolates cleanly.
RUN python3 -m venv /opt/alice-venv \
    && /opt/alice-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/alice-venv/bin/pip install --no-cache-dir \
        'claude-agent-sdk>=0.1.66' \
        'httpx>=0.28.1' \
        'mistune>=3.0' \
        'pyyaml>=6.0' \
        'discord.py>=2.4' \
        'a2a-sdk[http-server]>=1.0.2' \
        'jinja2>=3.1' \
        'fastapi>=0.115' \
        'uvicorn[standard]>=0.30' \
        'sse-starlette>=2.0'

# s6-overlay for process supervision. Arch-branched because the upstream
# tarball is per-arch; ADD can't expand a shell var mid-URL, so we curl.
# TARGETARCH is auto-populated by BuildKit from the host platform.
RUN case "${TARGETARCH}" in \
        amd64) S6_ARCH=x86_64 ;; \
        arm64) S6_ARCH=aarch64 ;; \
        *)     echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
         -o /tmp/s6-noarch.tar.xz \
    && curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
         -o /tmp/s6-arch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-arch.tar.xz \
    && rm /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz

# alice user — uid/gid match the host so bind-mount ownership lines up.
# -o on both groupadd and useradd: macOS hosts commonly have GID/UID values
# that collide with stock Debian entries (e.g. GID 20 = dialout). Allowing
# non-unique IDs is fine here — we never look up alice by ID, only by name.
RUN groupadd -o -g ${GROUP_ID} alice \
    && useradd -o -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/alice-mind /home/alice/alice-tools \
                /home/alice/.local/share/signal-cli \
                /state/worker /state/daemon \
    && chown -R alice:alice /home/alice /state

# Entrypoint + gitconfig. entrypoint.sh remains the worker entrypoint — its
# work (host-claude / host-codex auth dance + sidecar install.sh) is still
# needed in the folded image. The /host-home branch inside it is dead code
# in the single-container shape (that mount goes away in the new compose);
# kept here so this scaffold stays additive — drop it in a follow-up once
# the new docker-compose.yml lands and the legacy compose is retired.
COPY --chown=alice:alice sandbox/entrypoint.sh   /usr/local/bin/entrypoint.sh
COPY --chown=alice:alice sandbox/alice.gitconfig /home/alice/.gitconfig

# App wrappers are NOT baked into the image. They're symlinked from
# /home/alice/alice/bin/ (the bind-mounted monorepo) into /usr/local/bin/
# by entrypoint.sh at container start, so renames / module-path edits in
# the wrappers propagate to running containers without a docker rebuild.
# Issue #291: the alice_forge rename silently broke the sm-dispatcher
# because the baked-in /usr/local/bin/alice-sm-dispatcher still pointed
# at the old `sm.dispatcher` module path while the host source had
# already been updated to `alice_forge.dispatcher`.

# Merged s6 tree — daemon + worker + new alice-viewer unit.
COPY sandbox/s6/ /etc/s6-overlay/s6-rc.d/

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && find /etc/s6-overlay/s6-rc.d \( -name 'run' -o -name 'up' -o -name 'finish' \) | xargs -r chmod +x

ENV HOME=/home/alice
# User's personal sidecars (/home/alice/alice-tools/bin) shadow built-ins.
ENV PATH=/home/alice/alice-tools/bin:/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin
ENV S6_KEEP_ENV=1

# Viewer port published from the same container now; speaking + signal-cli
# stay on localhost (no inter-container DNS hop).
EXPOSE 7777

WORKDIR /home/alice/alice-mind

# Readiness = viewer responding AND speaking + thinking heartbeats fresh.
# Docker honors only one HEALTHCHECK per image, so the three probes combine
# in a single shell test:
#
#   - viewer `/` returns 2xx (cheapest visible signal that the pod is up).
#   - /state/worker/speaking-alive mtime within the last 300s. Speaking's
#     event-loop tick (SurfaceWatcher._run, 5s cadence) refreshes this
#     regardless of inbound traffic, so a wedged turn shows up as a stuck
#     mtime within ~6 min even when no Signal / surface activity is in flight.
#   - /state/worker/thinking-alive mtime within the last 600s. Thinking
#     touches this at the start of each wake (5-min cadence) before any
#     model call, so a wedged wake or stuck claude CLI subprocess is caught
#     within ~10 min.
#
# `2>/dev/null || echo 0` makes the staleness test FAIL (not error) when the
# liveness file doesn't exist yet — we want a stuck mtime to trip the probe,
# not a missing file to mask it. No try/except wrappers around the producer-
# side touches either: if the FS is read-only or the dir is missing, the
# probe SHOULD fire. Triggered by the 2026-05-28 hang (force-killed after
# orphaned claude/ssh processes had been stuck since 08:43 EDT — speaking +
# viewer both hung but the viewer-only HEALTHCHECK reported healthy); 9 such
# incidents from 2026-04-26 through 2026-05-28. See EKS Phase 2 design §2.
# /healthz (added 2026-05-28, #424) is a no-I/O liveness route: returns
# "ok" without touching the vault, so the probe is bounded by uvicorn
# scheduling rather than virtiofs cold-cache latency. The previous probe
# hit "/", which on macOS/Rancher-Desktop's virtiofs mount measured ~5s
# on first paint and tripped the old 3s wall every retry. Timeout bumped
# to 10s and start_period to 120s for belt-and-suspenders headroom on
# slow mounts and cold uvicorn imports.
HEALTHCHECK --interval=15s --timeout=10s --retries=5 --start-period=120s \
    CMD (curl -sf http://127.0.0.1:7777/healthz >/dev/null 2>&1 && \
         test $(($(date +%s) - $(stat -c %Y /state/worker/speaking-alive 2>/dev/null || echo 0))) -lt 300 && \
         test $(($(date +%s) - $(stat -c %Y /state/worker/thinking-alive 2>/dev/null || echo 0))) -lt 600) \
        || exit 1

ENTRYPOINT ["/init"]
CMD ["/usr/local/bin/entrypoint.sh", "sleep", "infinity"]
