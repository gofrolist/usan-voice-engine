# syntax=docker/dockerfile:1.7-labs
ARG BASE_IMAGE=usan-agent-base:local
FROM ${BASE_IMAGE}

WORKDIR /app

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root user
RUN groupadd -g 1001 appuser && \
    useradd  -u 1001 -g 1001 -s /sbin/nologin -d /app appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/app/.hf_cache \
    PYTHONPATH=/app/src

CMD ["python", "-m", "usan_agent.worker", "start"]
