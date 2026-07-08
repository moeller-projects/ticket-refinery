# syntax=docker/dockerfile:1.7
FROM node:24-bookworm-slim AS builder

ARG PI_VERSION=0.80.3

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv

RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"

WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv venv /venv && \
    VIRTUAL_ENV=/venv uv pip install -r requirements.txt

COPY src/ /app/src/

FROM node:24-bookworm-slim

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=builder /usr/local/bin/pi /usr/local/bin/pi
COPY --from=builder /venv /venv
# ponytail: venv's interpreter is a symlink into uv's managed Python store;
# copy that store too or every `python` invocation breaks.
COPY --from=builder /root/.local/share/uv /root/.local/share/uv
COPY --from=builder /app/src /app/src

ENV PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PATH="/venv/bin:${PATH}"

WORKDIR /app
CMD ["python", "-u", "src/refine.py"]