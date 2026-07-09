# syntax=docker/dockerfile:1.7
FROM node:24-bookworm-slim

ARG PI_VERSION=0.80.3

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv

# Pi CLI.
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"

WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv venv /venv && \
    VIRTUAL_ENV=/venv uv pip install -r requirements.txt

COPY src/ /app/src/

# Put the venv on PATH before registering the Graphify skill so the
# `graphify` console script (installed via `pip install graphifyy`)
# resolves correctly.
ENV PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PATH="/venv/bin:${PATH}"

# Register the Graphify skill with Pi so the agent can run
# `/graphify query / path / explain / affected` against the per-item
# `graph.json` index. graphifyy is installed via requirements.txt; this
# command copies the skill manifest into `/root/.pi/agent/skills/graphify`.
RUN graphify pi install

WORKDIR /app
CMD ["python", "-u", "src/refine.py"]
