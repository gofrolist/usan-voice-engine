# syntax=docker/dockerfile:1.7-labs
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first for cache-friendliness
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY docker-entrypoint.sh ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root user
RUN chmod +x docker-entrypoint.sh && \
    groupadd -g 1001 appuser && \
    useradd  -u 1001 -g 1001 -s /sbin/nologin -d /app appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
