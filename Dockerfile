# Stage 1: Build virtual environment with uv
FROM ghcr.io/astral-sh/uv:0.5.24 AS uv-bin
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY --from=uv-bin /uv /uvx /bin/

# Install build dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN uv pip install --no-cache .

# Stage 2: Runtime image
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

# Install tzdata and runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

# Copy virtualenv and code
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appgroup alembic.ini /app/
COPY --chown=appuser:appgroup migrations /app/migrations
COPY --chown=appuser:appgroup scripts /app/scripts
COPY --chown=appuser:appgroup docs /app/docs
COPY --chown=appuser:appgroup src /app/src

USER appuser

EXPOSE 8000

CMD ["uvicorn", "agrobot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
