FROM debian:trixie-slim

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG SIGNAL_CLI_VERSION=0.14.1
ARG S6_OVERLAY_VERSION=3.2.2.0

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV NODE_MAJOR=24

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git openssh-client jq less xz-utils \
        ripgrep fd-find tini procps \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest

# signal-cli (native GraalVM build — no JVM needed). Tarball contains a
# single binary named `signal-cli` — no wrapping directory.
RUN curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
        | tar -xz -C /usr/local/bin \
    && chmod +x /usr/local/bin/signal-cli

# s6-overlay for process supervision (daemon + bridge + autopush)
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz \
    && rm /tmp/s6-overlay-*.tar.xz

RUN groupadd -g ${GROUP_ID} alice \
    && useradd -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/alice-mind /home/alice/alice-tools \
    && chown -R alice:alice /home/alice

COPY --chown=alice:alice sandbox/entrypoint.sh   /usr/local/bin/entrypoint.sh
COPY --chown=alice:alice sandbox/alice.gitconfig /home/alice/.gitconfig
COPY sandbox/s6/ /etc/s6-overlay/s6-rc.d/

# Runtime scripts baked into the image (container-side only — the host-side
# wrappers `alice`, `alice-up`, `alice-shell`, `alice-down`, `alice-init`
# stay on the host).
COPY --chown=alice:alice bin/alice-mind-autopush /usr/local/bin/alice-mind-autopush
COPY --chown=alice:alice bin/event-log           /usr/local/bin/event-log
COPY --chown=alice:alice signal/signal-bridge.sh /usr/local/bin/signal-bridge

RUN chmod +x /usr/local/bin/entrypoint.sh \
        /usr/local/bin/alice-mind-autopush \
        /usr/local/bin/event-log \
        /usr/local/bin/signal-bridge \
    && find /etc/s6-overlay/s6-rc.d \( -name 'run' -o -name 'up' -o -name 'finish' \) | xargs -r chmod +x

ENV HOME=/home/alice
# User can prepend /home/alice/alice-tools/bin to PATH via their docker-compose
# override to shadow built-ins with personal sidecar scripts.
ENV PATH=/home/alice/alice-tools/bin:/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin
# s6 runs services; PID 1 is s6 init which handles signals + reaping properly.
ENV S6_KEEP_ENV=1

WORKDIR /home/alice/alice-mind

ENTRYPOINT ["/init"]
CMD ["/usr/local/bin/entrypoint.sh", "sleep", "infinity"]
