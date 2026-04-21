FROM debian:trixie-slim

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG SIGNAL_CLI_VERSION=0.14.1
ARG S6_OVERLAY_VERSION=3.2.2.0

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils procps \
    && rm -rf /var/lib/apt/lists/*

# signal-cli (native GraalVM build — no JVM needed).
RUN curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
        | tar -xz -C /usr/local/bin \
    && chmod +x /usr/local/bin/signal-cli

# s6-overlay
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz \
    && rm /tmp/s6-overlay-*.tar.xz

# alice user (uid matches the worker + host so shared volume ownership lines up)
RUN groupadd -g ${GROUP_ID} alice \
    && useradd -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/.local/share/signal-cli /state/daemon \
    && chown -R alice:alice /home/alice /state

COPY sandbox/daemon/s6/ /etc/s6-overlay/s6-rc.d/
RUN find /etc/s6-overlay/s6-rc.d \( -name 'run' -o -name 'up' -o -name 'finish' \) | xargs -r chmod +x

ENV HOME=/home/alice
ENV PATH=/usr/local/bin:/usr/bin:/bin
ENV S6_KEEP_ENV=1

HEALTHCHECK --interval=10s --timeout=3s --retries=3 --start-period=15s \
    CMD curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","method":"version","id":"hc"}' >/dev/null || exit 1

ENTRYPOINT ["/init"]
CMD ["sleep", "infinity"]
