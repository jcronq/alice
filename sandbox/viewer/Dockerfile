FROM debian:trixie-slim

ARG USER_ID=1000
ARG GROUP_ID=1000

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

ENV NODE_MAJOR=24

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg python3 python3-venv \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@latest

# claude-agent-sdk is needed by alice_viewer.run_summary, which spins up
# a tiny Haiku agent to summarize each completed wake.
RUN python3 -m venv /opt/alice-venv \
    && /opt/alice-venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/alice-venv/bin/pip install --no-cache-dir \
        'fastapi>=0.115' 'uvicorn[standard]>=0.30' 'jinja2>=3.1' \
        'sse-starlette>=2.0' 'httpx>=0.28.1' 'claude-agent-sdk>=0.1.66'

RUN groupadd -g ${GROUP_ID} alice \
    && useradd -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash alice \
    && mkdir -p /home/alice/alice /home/alice/alice-mind /state \
    && chown -R alice:alice /home/alice /state

USER alice
ENV HOME=/home/alice
ENV PATH=/opt/alice-venv/bin:/usr/local/bin:/usr/bin:/bin
ENV PYTHONPATH=/home/alice/alice/src
WORKDIR /home/alice/alice

EXPOSE 7777

HEALTHCHECK --interval=15s --timeout=3s --retries=3 --start-period=10s \
    CMD curl -sf http://127.0.0.1:7777/ >/dev/null || exit 1

CMD ["/opt/alice-venv/bin/python", "-m", "alice_viewer.main"]
