FROM node:24-bookworm-slim

# ponytail: pin PI_VERSION to a specific release for reproducible builds.
ARG PI_VERSION=0.80.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git python3 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv \
 && uv venv /venv \
 && npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force

ENV PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PATH="/venv/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --no-cache -r requirements.txt
COPY . .

CMD ["python", "-u", "src/refine.py"]
