# Runtime image for the monitor.
#
#   docker build -t solana-token-monitor .
#   docker run --rm --env-file .env solana-token-monitor
#
# Secrets are only ever passed through the environment; nothing is baked in.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

# Dependencies first so the layer is cached across source changes.
COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && useradd --create-home --uid 10001 monitor \
    && mkdir -p /data \
    && chown -R monitor:monitor /data /app

USER monitor

VOLUME ["/data"]

# Fails the container health check if the bot cannot report on itself.
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD ["solana-monitor", "--check-config"]

ENTRYPOINT ["solana-monitor"]
