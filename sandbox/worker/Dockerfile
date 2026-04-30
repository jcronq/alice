FROM debian:trixie-slim

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG S6_OVERLAY_VERSION=3.2.2.0
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV NODE_MAJOR=24

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git openssh-client jq less xz-utils \
        ripgrep fd-find tini procps util-linux python3 python3-venv python3-pip \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest

# Dedicated venv for alice (agent kernel + speaking daemon + thinking wake).
# The monorepo src/ tree is bind-mounted at /home/alice/alice/src and added
# to PYTHONPATH by the s6 services and /usr/local/bin/alice-think — so this
# venv only needs the upstream deps, not the alice_* packages themselves.
# Debian marks system pip as externally-managed; a venv isolates cleanly.
RUN python3 -m venv /opt/alice-venv \
    && /opt/alice-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/alice-venv/bin/pip install --no-cache-dir \
        'claude-agent-sdk>=0.1.66' \
        'httpx>=0.28.1' \
        'mistune>=3.0' \
        'pyyaml>=6.0' \
        'discord.py>=2.4' \
        'a2a-sdk[http-server]>=1.0.2' \
        'jinja2>=3.1'
# jinja2 is the prompt-template engine for ``alice_prompts``
# (Plan 04 of the runtime refactor). Listed alongside the other
# runtime deps because the worker bypasses ``uv sync`` and
# pip-installs directly, so pyproject.toml's deps don't reach
# this venv automatically.

# s6-overlay for process supervision (bridge + autopush). Arch-branched
# because the upstream tarball is per-arch; ADD can't expand a shell var
# mid-URL, so we curl. TARGETARCH is auto-populated by BuildKit from the
# host platform (amd64 on x86_64 hosts, arm64 on aarch64 like DGX Spark).
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

# -o allows the GID/UID to collide with a stock Debian entry (macOS GID 20
# overlaps `dialout`, etc.). Lookups are by name, so collisions are harmless.
RUN groupadd -o -g ${GROUP_ID} alice \
    && useradd -o -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/alice-mind /home/alice/alice-tools /state/worker \
    && chown -R alice:alice /home/alice /state

COPY --chown=alice:alice sandbox/entrypoint.sh   /usr/local/bin/entrypoint.sh
COPY --chown=alice:alice sandbox/alice.gitconfig /home/alice/.gitconfig
COPY sandbox/worker/s6/ /etc/s6-overlay/s6-rc.d/

# Runtime scripts baked into the image (host-side wrappers stay on host).
COPY --chown=alice:alice bin/alice-mind-autopush /usr/local/bin/alice-mind-autopush
COPY --chown=alice:alice bin/event-log           /usr/local/bin/event-log
COPY --chown=alice:alice bin/alice-think         /usr/local/bin/alice-think
COPY --chown=alice:alice bin/alice-client        /usr/local/bin/alice-client
COPY --chown=alice:alice bin/alice-gh-watcher    /usr/local/bin/alice-gh-watcher

RUN chmod +x /usr/local/bin/entrypoint.sh \
        /usr/local/bin/alice-mind-autopush \
        /usr/local/bin/event-log \
        /usr/local/bin/alice-think \
        /usr/local/bin/alice-client \
        /usr/local/bin/alice-gh-watcher \
    && find /etc/s6-overlay/s6-rc.d \( -name 'run' -o -name 'up' -o -name 'finish' \) | xargs -r chmod +x

ENV HOME=/home/alice
# User's personal sidecars (/home/alice/alice-tools/bin) shadow built-ins.
ENV PATH=/home/alice/alice-tools/bin:/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin
ENV S6_KEEP_ENV=1

WORKDIR /home/alice/alice-mind

# Readiness = this container's bridge holds the exclusive flock on the
# lease file (i.e., this worker is either live or actively blue/green-
# swapping with a peer that's about to hand off).
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=30s \
    CMD sh -c '! flock -n /state/worker/lease true' || exit 1

ENTRYPOINT ["/init"]
CMD ["/usr/local/bin/entrypoint.sh", "sleep", "infinity"]
