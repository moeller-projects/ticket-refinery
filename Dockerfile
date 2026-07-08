# syntax=docker/dockerfile:1.7
FROM node:24-bookworm-slim AS builder

ARG PI_VERSION=0.80.3
ARG CODEGRAPH_VERSION=v1.0.0
ARG PI_CODEGRAPH_EXT_VERSION=0.1.10

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv venv /venv && \
    VIRTUAL_ENV=/venv uv pip install -r requirements.txt

COPY src/ /app/src/

FROM node:24-bookworm-slim

ARG PI_VERSION=0.80.3
ARG PI_CODEGRAPH_EXT_VERSION=0.1.10

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv

# CodeGraph CLI: official standalone installer (vendored Node + binary bundle;
# no Node / npm / build tools required). Pinned to /opt so the runtime stage
# can COPY it across stages without chasing the version dirs.
ENV CODEGRAPH_INSTALL_DIR=/opt/codegraph \
    CODEGRAPH_BIN_DIR=/usr/local/bin \
    CODEGRAPH_VERSION=${CODEGRAPH_VERSION}
RUN curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh

RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    pi install "@vndv/pi-codegraph@${PI_CODEGRAPH_EXT_VERSION}"

COPY --from=builder /venv /venv
COPY --from=builder /root/.local/share/uv /root/.local/share/uv
COPY --from=builder /app/src /app/src

# Ponytail: bake a minimal Pi MCP config so the codegraph MCP server is
# reachable inside the container. The host's ~/.pi/agent/mcp.json is left
# alone (run.ps1 mounts only auth.json, not MCP config). Without this,
# `pi install` may register the extension but Pi won't be able to spawn
# the MCP server from inside the image.
# RUN mkdir -p /root/.pi/agent && \
#     printf '%s\n' \
#       '{"mcpServers":{' \
#       '"codegraph":{"command":"codegraph","args":["serve","--mcp"],"lifecycle":"lazy"}' \
#       '}}' > /root/.pi/agent/mcp.json

ENV PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PATH="/venv/bin:${PATH}"

WORKDIR /app
CMD ["python", "-u", "src/refine.py"]