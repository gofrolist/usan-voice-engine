# syntax=docker/dockerfile:1.7-labs

# ── Builder ──────────────────────────────────────────────────────────────────
# Resolves the locked dependency set into a self-contained /app/.venv. uv and its
# download caches live ONLY in this stage and never reach the runtime image.
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS builder

# No UV_COMPILE_BYTECODE: precompiling .pyc adds ~57MB to the image for only a
# sub-second, once-per-(re)start import gain on a long-lived container — a poor
# trade when footprint is the priority. Runtime sets PYTHONDONTWRITEBYTECODE so
# the read-only rootfs never attempts futile .pyc writes.
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# Install uv (pinned by digest).
COPY --from=ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first — this layer only busts when pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then the project itself. --no-editable builds a real wheel into the venv so the
# package lives entirely inside .venv/site-packages — no source tree has to ship
# at a magic path, and the runtime image needs no separate src/ copy.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ── Runtime ──────────────────────────────────────────────────────────────────
# Clean slim base — no uv, no build caches. Just the interpreter, the venv and the
# migration assets. Same pinned digest as the builder so the venv's interpreter
# symlink resolves.
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# The non-root user must exist BEFORE the COPY --chown below: chowning at copy
# time keeps ownership in the same layer, whereas a post-hoc `chown -R /app`
# would duplicate the entire venv into a fresh (large) layer.
RUN groupadd -g 1001 appuser && \
    useradd  -u 1001 -g 1001 -s /sbin/nologin -d /app appuser

# The venv (with the project installed as a real wheel + precompiled bytecode) must
# land at the SAME path the builder used so its interpreter symlink resolves. The
# app code is inside the venv now — only the alembic migration assets (read at
# startup by `alembic upgrade head`, not imported) need to ship separately.
COPY --from=builder --chown=appuser:appuser /app/.venv ./.venv
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser --chmod=0755 docker-entrypoint.sh ./

USER appuser

EXPOSE 8000

# /health is dependency-free (no DB hit); slim has no curl/wget, so probe via the
# stdlib interpreter already on PATH (the venv python).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# OCI provenance — traceability from a running container back to the git SHA/tag
# (build.yml feeds GIT_SHA/VERSION; local builds default to "dev").
ARG GIT_SHA=dev
ARG VERSION=dev
# Promote to ENV so the running process (Settings.app_version / git_sha) can report the
# deployed version at runtime via /health and /v1/auth/me — not just as an OCI label.
ENV APP_VERSION=${VERSION} \
    GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.source="https://github.com/gofrolist/usan-voice-engine" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="usan-api"

ENTRYPOINT ["./docker-entrypoint.sh"]
