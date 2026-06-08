# syntax=docker/dockerfile:1.7-labs
# Stage 1 — build the static SPA bundle (tsc typecheck + vite build).
FROM node:22-alpine AS build
WORKDIR /app
# Install deps first for cache-friendliness (lockfile-driven, reproducible).
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# Stage 2 — serve the built assets with Caddy (no TLS here; the edge Caddy fronts
# this), non-root, on an unprivileged port. Mirrors the stack's Caddy usage.
FROM caddy:2-alpine AS serve
# Writable XDG dirs so a non-root Caddy starts cleanly (it never writes certs here
# with auto_https/persist_config off, but the dirs must exist + be owned).
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data
RUN mkdir -p /srv /config /data \
 && addgroup -g 1001 appuser \
 && adduser -D -u 1001 -G appuser appuser \
 && chown -R 1001:1001 /srv /config /data
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
USER 1001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/ >/dev/null 2>&1 || exit 1
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
