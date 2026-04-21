FROM debian:trixie-slim

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG S6_OVERLAY_VERSION=3.2.2.0

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV NODE_MAJOR=24

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git openssh-client jq less xz-utils \
        ripgrep fd-find tini procps util-linux python3 \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest

# s6-overlay for process supervision (bridge + autopush)
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz \
    && rm /tmp/s6-overlay-*.tar.xz

RUN groupadd -g ${GROUP_ID} alice \
    && useradd -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/alice-mind /home/alice/alice-tools /state/worker \
    && chown -R alice:alice /home/alice /state

COPY --chown=alice:alice sandbox/entrypoint.sh   /usr/local/bin/entrypoint.sh
COPY --chown=alice:alice sandbox/alice.gitconfig /home/alice/.gitconfig
COPY sandbox/worker/s6/ /etc/s6-overlay/s6-rc.d/

# Runtime scripts baked into the image (host-side wrappers stay on host).
COPY --chown=alice:alice bin/alice-mind-autopush /usr/local/bin/alice-mind-autopush
COPY --chown=alice:alice bin/event-log           /usr/local/bin/event-log
COPY --chown=alice:alice signal/signal-bridge.sh /usr/local/bin/signal-bridge

RUN chmod +x /usr/local/bin/entrypoint.sh \
        /usr/local/bin/alice-mind-autopush \
        /usr/local/bin/event-log \
        /usr/local/bin/signal-bridge \
    && find /etc/s6-overlay/s6-rc.d \( -name 'run' -o -name 'up' -o -name 'finish' \) | xargs -r chmod +x

ENV HOME=/home/alice
# User's personal sidecars (/home/alice/alice-tools/bin) shadow built-ins.
ENV PATH=/home/alice/alice-tools/bin:/home/alice/.local/bin:/usr/local/bin:/usr/bin:/bin
ENV S6_KEEP_ENV=1

WORKDIR /home/alice/alice-mind

# Readiness = bridge has acquired lease and is processing messages.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=30s \
    CMD test -f /state/worker/lease-held || exit 1

ENTRYPOINT ["/init"]
CMD ["/usr/local/bin/entrypoint.sh", "sleep", "infinity"]
