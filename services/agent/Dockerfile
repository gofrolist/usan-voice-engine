# syntax=docker/dockerfile:1.7-labs
ARG BASE_IMAGE=usan-agent-base:local
FROM ${BASE_IMAGE}

# appuser, /app ownership, UV_CACHE_DIR/HF_HOME/HOME and USER are all inherited
# from the base. No recursive chown here — that would copy-up (duplicate) the
# whole venv + ~460MB model cache into a fresh layer on every code change.
WORKDIR /app

COPY --chown=appuser:appuser src/ ./src/
RUN --mount=type=cache,target=/app/.uv-cache,uid=1001,gid=1001 \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src

# OCI provenance — traceability from a running container back to the git SHA/tag.
ARG GIT_SHA=dev
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/gofrolist/usan-voice-engine" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="usan-agent"

# Liveness: the LiveKit Agents worker serves a health endpoint on :8081 in `start`
# (production) mode. A wedged worker — registered with LiveKit but no longer
# processing — fails this probe. Note: compose's restart policy does not act on
# health status, so true auto-restart needs an external watcher; this still
# surfaces the unhealthy signal to `docker ps` / monitoring.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/', timeout=3).status == 200 else 1)"]

CMD ["python", "-m", "usan_agent.worker", "start"]
