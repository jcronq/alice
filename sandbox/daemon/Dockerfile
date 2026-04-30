FROM debian:trixie-slim

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG SIGNAL_CLI_VERSION=0.14.1
ARG S6_OVERLAY_VERSION=3.2.2.0
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils procps \
    && rm -rf /var/lib/apt/lists/*

# signal-cli — arch-branched. amd64 uses the GraalVM native build (fast
# cold start, no JVM in the image). arm64 has no native build upstream,
# so install OpenJDK 25 from trixie main and use the JRE-based tarball.
# (signal-cli 0.14.1 targets JavaVersion.VERSION_25; lower JDKs hit
# UnsupportedClassVersionError at load.)
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

# s6-overlay — see worker/Dockerfile for the rationale on curl-vs-ADD.
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

# alice user (uid matches the worker + host so shared volume ownership lines up).
# -o on both groupadd and useradd: macOS hosts commonly have GID/UID values
# that collide with stock Debian entries (e.g. GID 20 = dialout). Allowing
# non-unique IDs is fine here — we never look up alice by ID, only by name.
RUN groupadd -o -g ${GROUP_ID} alice \
    && useradd -o -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/.local/share/signal-cli /state/daemon \
    && chown -R alice:alice /home/alice /state

COPY sandbox/daemon/s6/ /etc/s6-overlay/s6-rc.d/
RUN find /etc/s6-overlay/s6-rc.d \( -name 'run' -o -name 'up' -o -name 'finish' \) | xargs -r chmod +x

ENV HOME=/home/alice
ENV PATH=/usr/local/bin:/usr/bin:/bin
ENV S6_KEEP_ENV=1

HEALTHCHECK --interval=10s --timeout=3s --retries=3 --start-period=45s \
    CMD sh -c '[ -f /state/daemon/signal-disabled ] || curl -sf -X POST http://127.0.0.1:8080/api/v1/rpc -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"method\":\"version\",\"id\":\"hc\"}" >/dev/null' || exit 1

ENTRYPOINT ["/init"]
CMD ["sleep", "infinity"]
