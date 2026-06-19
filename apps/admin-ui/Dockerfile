# syntax=docker/dockerfile:1.7-labs
# Stage 1 — build the static SPA bundle (tsc typecheck + vite build).
# Base pinned by digest (immutable) — the human tag is kept in the comment.
# Dependabot's docker ecosystem bumps digest-pinned FROM lines in Dockerfiles.
FROM node:22-alpine@sha256:5e8888a165087a80513a7e773bb1a60c2e7dd54ac7cddab404ae2f470815e8e8 AS build
WORKDIR /app
# Install deps first for cache-friendliness (lockfile-driven, reproducible).
# --no-audit/--no-fund skip network registry chatter that only slows CI builds.
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# Stage 2 — serve the built assets with Caddy (no TLS here; the edge Caddy fronts
# this), non-root, on an unprivileged port. Mirrors the stack's Caddy usage.
FROM caddy:2-alpine@sha256:4679412ccd2114dca9f3017e2c460f76c9d8eb9e1850e72fb9ecda672e05c629 AS serve
# Writable XDG dirs so a non-root Caddy starts cleanly (it never writes certs here
# with auto_https/persist_config off, but the dirs must exist + be owned). Run as the
# numeric UID 1001 directly — no named account is needed and creating one risks a
# GID collision with the base image; chown by numeric id works regardless.
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data
RUN mkdir -p /srv /config /data && chown -R 1001:1001 /srv /config /data
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
USER 1001
EXPOSE 8080
# --spider probes the endpoint without downloading the body (lighter than -qO-).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget --spider -q http://127.0.0.1:8080/ || exit 1
# OCI provenance — traceability from a running container back to the git SHA/tag.
ARG GIT_SHA=dev
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/gofrolist/usan-voice-engine" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="usan-admin-ui"
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
