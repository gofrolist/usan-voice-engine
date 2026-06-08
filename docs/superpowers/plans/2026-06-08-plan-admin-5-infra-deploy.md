# Admin UI P5 — Infra & Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the admin console (P1–P4) live and reachable: a static-serving container for the SPA, a Caddy `admin.<domain>` site gated to an operator CIDR, the compose env wiring that finally delivers the P3 SSO settings to the API container, Terraform DNS, CI image build + frontend gate, and a documented secret/OAuth deploy runbook.

**Architecture:** Mirror the existing Grafana "internal web UI behind Caddy + CIDR" pattern exactly. The SPA builds to static assets baked into a tiny `usan-admin-ui` image (Caddy `file_server` with SPA `try_files` fallback, non-root, no published port). The edge Caddy adds an `{$ADMIN_DOMAIN}` site block: a single L7 `remote_ip` allowlist gates the whole origin; `/v1/*` proxies to `api:8000` and everything else to `admin-ui:8080`, so the SPA and API are **same-origin** (the SameSite=Strict session cookie works with no CORS). Going live requires **both** a `v*` tag (app/compose) **and** `terraform apply` (DNS), plus the new secret keys seeded onto the VM **before** the tag deploy.

**Tech Stack:** Docker Compose overlays, Caddy 2, Terraform (Cloudflare DNS), GitHub Actions, Vite static build, pytest (stdlib + pyyaml structural contract).

**Scope:** `infra`, `ci`. **No** `apps/api` Python source change beyond compose env passthroughs; **no** `services/agent` change (the `apps/api ⊥ services/agent` boundary stays intact). P3 already shipped the API SSO code and settings fields; this phase only *delivers* those settings into the running container and exposes the SPA.

---

## Context the implementer needs

- The session settings already exist (`apps/api/src/usan_api/settings.py`): `google_oauth_client_id` (`GOOGLE_OAUTH_CLIENT_ID`), `google_oauth_client_secret` (`GOOGLE_OAUTH_CLIENT_SECRET`), `google_oauth_redirect_uri` (`GOOGLE_OAUTH_REDIRECT_URI`), `google_oauth_hd` (`GOOGLE_OAUTH_HD`), `admin_bootstrap_emails` (`ADMIN_BOOTSTRAP_EMAILS`), `admin_session_ttl_s` (`ADMIN_SESSION_TTL_S`, default 28800), `session_cookie_secure` (`SESSION_COOKIE_SECURE`, default `true`), `admin_post_login_redirect` (`ADMIN_POST_LOGIN_REDIRECT`, default `/`). They are all optional; `sso_enabled` is true only when id+secret+redirect are all set. **The base `api` compose service does not pass any of them through, so today they never reach the container.** This phase fixes that.
- The SPA (`apps/admin-ui`) builds with `npm run build` (`tsc --noEmit && vite build`) → `dist/`. Entry is `index.html`; client-side routing means unknown paths must fall back to `/index.html`. `.gitignore` already excludes `node_modules`, `dist`, `coverage`. A `package-lock.json` is committed (`npm ci` works).
- Templates to mirror verbatim where possible:
  - **Grafana service** in `infra/docker-compose.monitoring.yml` (image with `${IMAGE_REGISTRY:-…}` default + `${IMAGE_TAG:?…}`, `pull_policy: always`, `logging.driver: journald`, no published port, `restart: unless-stopped`).
  - **Grafana site block** in `infra/Caddyfile` (`@operator remote_ip {$GRAFANA_ALLOWED_CIDR}` / `handle @operator { … }` / `respond 403`).
  - **Grafana DNS record** in `infra/terraform/dns.tf` (`count = local.manage_dns ? 1 : 0`, `proxied = false`, `content = google_compute_address.usan.address`).
  - **api build step** in `.github/workflows/build.yml` (GHCR + GAR tag matrix, `cache-from/to scope=…`).
- The prod compose chain (deploy job in `build.yml` + README) is, in order:
  `-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml -f docker-compose.monitoring.yml`. P5 appends `-f docker-compose.admin.yml` (last).
- Compose `environment:` **maps** merge across overlays (the prod overlay already "merges with the base file's environment map"). The base `api` service uses the map form, so adding more keys there is additive.
- `ADMIN_DOMAIN` / `ADMIN_ALLOWED_CIDR` are **Caddy** env vars carried in the `.env` secret (exactly like `GRAFANA_DOMAIN`/`GRAFANA_ALLOWED_CIDR`) — **not** Terraform variables. The DNS record name is hardcoded `admin`, like `grafana`.
- Contract tests live in `scripts/tests/` and run in the `pytest (scripts)` CI job (`pip install pytest`, Python 3.12, repo-root = `Path(__file__).resolve().parents[2]`). The dashboards validator is pure stdlib (JSON); this phase parses YAML, so add `pyyaml` to that one job's install step.
- Known deploy sharp edges (do not rediscover): the `v*` tag deploy runs `compose up --env-file infra/.env` but **never re-fetches the secret**, so new `.env` keys must be seeded onto `/opt/usan/infra/.env` (reboot or IAP-SSH) **before** the tag deploy. IAP SSH works even when your IP isn't in `operator_ssh_cidr`.

## File structure

- Create `apps/admin-ui/Dockerfile` — multi-stage: node build → `caddy:2-alpine` static server.
- Create `apps/admin-ui/.dockerignore` — keep `node_modules`/`dist`/test noise out of the build context.
- Create `apps/admin-ui/Caddyfile` — the **inner** static-server config (SPA fallback), baked into the image.
- Create `infra/docker-compose.admin.yml` — the admin overlay (`admin-ui` service + `caddy` env additions).
- Modify `infra/docker-compose.yml` — add the 8 SSO/admin env passthroughs to `api.environment`.
- Modify `infra/Caddyfile` — append the `{$ADMIN_DOMAIN}` site block.
- Modify `infra/terraform/dns.tf` — append the `admin` Cloudflare A record.
- Modify `.github/workflows/build.yml` — admin-ui build step + deploy SCP/compose-chain wiring.
- Modify `.github/workflows/test.yml` — `vitest (apps/admin-ui)` job; add `pyyaml` to `pytest (scripts)`.
- Modify `.github/workflows/lint.yml` — `Lint admin-ui (apps/admin-ui)` job (eslint + typecheck).
- Modify `infra/.env.prod.example` and `infra/.env.example` — document the new keys.
- Modify `infra/README.md` — admin-UI deploy runbook + corrected full compose chain.
- Create `scripts/tests/test_admin_infra.py` — structural contract over all of the above.

---

### Task 1: Contract tests (write first — RED)

**Files:**
- Create: `scripts/tests/test_admin_infra.py`
- Modify: `.github/workflows/test.yml` (add `pyyaml` to the scripts job install)

The contract encodes the invariants the rest of the tasks must satisfy. Parse the
real files; assert intent that `docker compose config` alone would not catch (e.g.
"no published port").

- [ ] **Step 1: Write the contract test**

```python
"""Structural contract for the admin-UI infra/deploy wiring (Plan admin-5).

Runs in the `pytest (scripts)` CI job (Python 3.12 + pytest + pyyaml). Pins the
P5 invariants so a regression in the compose overlay, Caddyfile, CI workflow,
Terraform DNS, or env docs fails CI before it can reach the VM.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def _load_yaml(rel: str):
    return yaml.safe_load((ROOT / rel).read_text())


def test_admin_overlay_service_shape():
    doc = _load_yaml("infra/docker-compose.admin.yml")
    svc = doc["services"]["admin-ui"]
    assert "usan-admin-ui" in svc["image"]
    assert "${IMAGE_TAG" in svc["image"]  # explicit-tag required
    assert svc["pull_policy"] == "always"
    assert svc["logging"]["driver"] == "journald"
    assert svc["restart"] == "unless-stopped"
    # No published port: Caddy reaches it on the bridge.
    assert "ports" not in svc


def test_admin_overlay_caddy_env():
    doc = _load_yaml("infra/docker-compose.admin.yml")
    env = doc["services"]["caddy"]["environment"]
    keys = env.keys() if isinstance(env, dict) else {e.split("=", 1)[0] for e in env}
    assert "ADMIN_DOMAIN" in keys
    assert "ADMIN_ALLOWED_CIDR" in keys


def test_api_env_passes_sso_settings():
    doc = _load_yaml("infra/docker-compose.yml")
    env = doc["services"]["api"]["environment"]
    keys = env.keys() if isinstance(env, dict) else {e.split("=", 1)[0] for e in env}
    for k in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
        "GOOGLE_OAUTH_HD",
        "ADMIN_BOOTSTRAP_EMAILS",
        "ADMIN_SESSION_TTL_S",
        "SESSION_COOKIE_SECURE",
        "ADMIN_POST_LOGIN_REDIRECT",
    ):
        assert k in keys, f"api service must pass {k} to the container"


def test_caddyfile_admin_block_is_cidr_gated():
    text = (INFRA / "Caddyfile").read_text()
    assert "{$ADMIN_DOMAIN}" in text
    assert "remote_ip {$ADMIN_ALLOWED_CIDR}" in text
    # Same-origin: /v1 to the API, everything else to the SPA container.
    assert "admin-ui:8080" in text
    # The 403 gate must exist in the admin block (default-deny outside the CIDR).
    admin_block = text.split("{$ADMIN_DOMAIN}", 1)[1]
    assert "respond 403" in admin_block


def test_inner_caddyfile_has_spa_fallback():
    text = (ROOT / "apps/admin-ui/Caddyfile").read_text()
    assert "try_files" in text and "/index.html" in text
    assert ":8080" in text


def test_dockerfile_runs_nonroot_static_server():
    text = (ROOT / "apps/admin-ui/Dockerfile").read_text()
    assert "vite build" in text or "npm run build" in text
    assert "caddy:2-alpine" in text
    assert re.search(r"USER\s+1001", text)


def test_build_workflow_builds_and_ships_admin_ui():
    text = (ROOT / ".github/workflows/build.yml").read_text()
    assert "usan-admin-ui" in text
    assert "scope=admin-ui" in text
    # Deploy job ships the overlay and includes it in the compose chain.
    assert "docker-compose.admin.yml" in text
    assert text.count("docker-compose.admin.yml") >= 2  # SCP source + -f chain


def test_frontend_ci_jobs_exist():
    test_yml = (ROOT / ".github/workflows/test.yml").read_text()
    lint_yml = (ROOT / ".github/workflows/lint.yml").read_text()
    assert "apps/admin-ui" in test_yml and "npm" in test_yml
    assert "apps/admin-ui" in lint_yml and "npm run lint" in lint_yml
    # The scripts job needs pyyaml for this very test.
    assert "pyyaml" in test_yml


def test_terraform_has_admin_dns_record():
    text = (INFRA / "terraform/dns.tf").read_text()
    assert 'cloudflare_dns_record" "admin"' in text
    block = text.split('"admin"', 1)[1]
    assert 'name    = "admin"' in block
    assert "proxied = false" in block


def test_env_examples_document_admin_keys():
    prod = (INFRA / ".env.prod.example").read_text()
    for k in (
        "ADMIN_DOMAIN",
        "ADMIN_ALLOWED_CIDR",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
        "ADMIN_BOOTSTRAP_EMAILS",
    ):
        assert k in prod, f".env.prod.example must document {k}"
    assert "/v1/auth/callback" in prod  # the exact redirect URI shape
```

- [ ] **Step 2: Add `pyyaml` to the scripts CI job**

In `.github/workflows/test.yml`, the `pytest-scripts` job step:
```yaml
      - name: Install pytest
        run: pip install pytest pyyaml
```

- [ ] **Step 3: Run — expect RED**

Run: `python -m pytest scripts/tests/test_admin_infra.py -v`
Expected: failures / FileNotFoundError (the overlay, inner Caddyfile, Dockerfile, admin DNS, env keys, and CI jobs do not exist yet).

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/test_admin_infra.py .github/workflows/test.yml
git commit -m "test(infra): admin-5 infra contract (RED) + pyyaml in scripts CI"
```

---

### Task 2: admin-ui static image

**Files:**
- Create: `apps/admin-ui/Dockerfile`
- Create: `apps/admin-ui/.dockerignore`
- Create: `apps/admin-ui/Caddyfile`

- [ ] **Step 1: Inner Caddyfile (SPA static server)**

`apps/admin-ui/Caddyfile`:
```
{
	# No TLS at this layer (the edge Caddy terminates TLS); no admin API; do not
	# try to persist autosaved config (keeps the container working read-only/non-root).
	admin off
	auto_https off
	persist_config off
}

:8080 {
	root * /srv
	encode zstd gzip
	# SPA client-side routing: serve the file if it exists, else index.html.
	try_files {path} /index.html
	file_server
	# Long-cache the fingerprinted assets; never cache the HTML shell.
	@assets path /assets/*
	header @assets Cache-Control "public, max-age=31536000, immutable"
	header /index.html Cache-Control "no-cache"
}
```

- [ ] **Step 2: .dockerignore**

`apps/admin-ui/.dockerignore`:
```
node_modules
dist
coverage
e2e
.eslintignore
.eslintrc.cjs
*.local
.DS_Store
Dockerfile
.dockerignore
```

- [ ] **Step 3: Dockerfile (multi-stage, non-root)**

`apps/admin-ui/Dockerfile`:
```dockerfile
# syntax=docker/dockerfile:1.7-labs
# Stage 1 — build the static SPA bundle.
FROM node:22-alpine AS build
WORKDIR /app
# Install deps first for cache-friendliness (lockfile-driven, reproducible).
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# Stage 2 — serve the built assets with Caddy (no TLS; edge Caddy fronts this),
# non-root, on an unprivileged port. Mirrors the stack's Caddy usage.
FROM caddy:2-alpine AS serve
# Writable dirs so a non-root Caddy can start cleanly under read-only assumptions.
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
```

- [ ] **Step 4: Build it locally to verify**

Run: `docker build -t usan-admin-ui:local apps/admin-ui`
Expected: build succeeds; both stages complete.

Verify it serves + falls back:
```bash
docker run --rm -d --name admintest -p 18080:8080 usan-admin-ui:local
sleep 2
curl -fsS http://localhost:18080/ | grep -q '<div id="root">' && echo "root ok"
# Deep client-route must fall back to index.html (200, not 404):
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:18080/profiles/abc/versions
docker rm -f admintest
```
Expected: `root ok`; the deep route returns `200`.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/Dockerfile apps/admin-ui/.dockerignore apps/admin-ui/Caddyfile
git commit -m "feat(infra): admin-ui static-serving image (caddy file_server, non-root, SPA fallback)"
```

---

### Task 3: Compose admin overlay + api env passthroughs

**Files:**
- Create: `infra/docker-compose.admin.yml`
- Modify: `infra/docker-compose.yml` (api `environment:` additions)

- [ ] **Step 1: Add the SSO/admin env passthroughs to the base api service**

In `infra/docker-compose.yml`, inside `services.api.environment`, after the
existing `OPERATOR_API_KEY` line, add (`:-` defaults keep them optional so dev/test
are unaffected when unset; `SESSION_COOKIE_SECURE` defaults prod-safe `true`):
```yaml
      # Admin UI / Google SSO (Plan admin-3 code; admin-5 wires it through).
      # All optional — SSO is off until the client id+secret+redirect are all set.
      GOOGLE_OAUTH_CLIENT_ID: ${GOOGLE_OAUTH_CLIENT_ID:-}
      GOOGLE_OAUTH_CLIENT_SECRET: ${GOOGLE_OAUTH_CLIENT_SECRET:-}
      GOOGLE_OAUTH_REDIRECT_URI: ${GOOGLE_OAUTH_REDIRECT_URI:-}
      GOOGLE_OAUTH_HD: ${GOOGLE_OAUTH_HD:-}
      ADMIN_BOOTSTRAP_EMAILS: ${ADMIN_BOOTSTRAP_EMAILS:-}
      ADMIN_SESSION_TTL_S: ${ADMIN_SESSION_TTL_S:-28800}
      SESSION_COOKIE_SECURE: ${SESSION_COOKIE_SECURE:-true}
      ADMIN_POST_LOGIN_REDIRECT: ${ADMIN_POST_LOGIN_REDIRECT:-/}
```

- [ ] **Step 2: Create the admin overlay**

`infra/docker-compose.admin.yml`:
```yaml
# Admin UI overlay (prod) — the React SPA static container, fronted by the edge
# Caddy at ADMIN_DOMAIN and gated to ADMIN_ALLOWED_CIDR at L7 (same pattern as
# Grafana). Layer LAST, after the tls + monitoring overlays:
#   docker compose --env-file infra/.env \
#     -f infra/docker-compose.yml \
#     -f infra/docker-compose.prod.yml \
#     -f infra/docker-compose.tls.yml \
#     -f infra/docker-compose.monitoring.yml \
#     -f infra/docker-compose.admin.yml up -d
#
# Requires in .env: ADMIN_DOMAIN, ADMIN_ALLOWED_CIDR, and (for SSO to function)
# GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI + ADMIN_BOOTSTRAP_EMAILS (those reach
# the api via the base file's environment map).
services:
  admin-ui:
    image: ${IMAGE_REGISTRY:-us-east1-docker.pkg.dev/usan-retirement/usan}/usan-admin-ui:${IMAGE_TAG:?IMAGE_TAG must be set to an explicit tag}
    container_name: usan-admin-ui
    init: true
    pull_policy: always
    # Static assets only; no inbound ports published — the edge Caddy reaches it on
    # the bridge as admin-ui:8080 (operator-CIDR gated at the edge). Block privilege
    # escalation; the image already runs as non-root 1001.
    security_opt:
      - "no-new-privileges:true"
    restart: unless-stopped
    # Ship stdout to journald so the Ops Agent ingests it into Cloud Logging.
    logging:
      driver: journald

  # The edge Caddy (defined in the tls overlay) gains the admin.<domain> site env.
  # Merges with the tls overlay's caddy.environment map.
  caddy:
    environment:
      ADMIN_DOMAIN: ${ADMIN_DOMAIN}
      ADMIN_ALLOWED_CIDR: ${ADMIN_ALLOWED_CIDR:?ADMIN_ALLOWED_CIDR must be set to an operator CIDR allowlist}
```

- [ ] **Step 3: Validate the merged compose**

Run (with throwaway env so interpolation resolves):
```bash
cd infra
ADMIN_DOMAIN=admin.example ADMIN_ALLOWED_CIDR=203.0.113.4/32 IMAGE_TAG=vtest \
GRAFANA_ALLOWED_CIDR=203.0.113.4/32 \
docker compose --env-file /dev/null \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
  -f docker-compose.monitoring.yml -f docker-compose.admin.yml config >/dev/null \
  && echo "compose OK"
```
Expected: `compose OK` (the merged config parses; `admin-ui` resolves to the GAR
image at tag `vtest`; the api environment includes the SSO keys).
> If unset required vars abort `config`, export throwaway values for them as above.
> The point is to prove the overlay merges and interpolates, not to run anything.

- [ ] **Step 4: Run the relevant contract tests**

Run: `python -m pytest scripts/tests/test_admin_infra.py -k "overlay or api_env" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/docker-compose.admin.yml infra/docker-compose.yml
git commit -m "feat(infra): docker-compose.admin.yml overlay + wire SSO env into api"
```

---

### Task 4: Caddy admin site block

**Files:**
- Modify: `infra/Caddyfile`

- [ ] **Step 1: Append the admin site block**

Append to `infra/Caddyfile`:
```
{$ADMIN_DOMAIN} {
	encode zstd gzip
	# Operator-CIDR allowlist enforced at L7 (443 is SNI-shared across all sites,
	# so this cannot be a VM firewall rule). Outside the CIDR -> 403. The admin SPA
	# and the API share THIS origin, so the SameSite=Strict session cookie set by
	# /v1/auth works (no cross-site cookie, no CORS).
	@operator remote_ip {$ADMIN_ALLOWED_CIDR}
	handle @operator {
		# Same-origin API: admin/auth/runtime endpoints go to the api container.
		@api path /v1/*
		handle @api {
			reverse_proxy api:8000 {
				# Real (non-spoofable) client IP for the API rate limiter.
				header_up X-Forwarded-For {remote_host}
			}
		}
		# Everything else is the React SPA static container (client-side routing is
		# handled inside that container via try_files -> /index.html).
		handle {
			reverse_proxy admin-ui:8080
		}
	}
	respond 403
}
```

- [ ] **Step 2: Validate the Caddyfile**

Run:
```bash
docker run --rm -v "$PWD/infra/Caddyfile":/etc/caddy/Caddyfile:ro \
  -e API_DOMAIN=api.example -e LIVEKIT_DOMAIN=lk.example \
  -e GRAFANA_DOMAIN=grafana.example -e GRAFANA_ALLOWED_CIDR=203.0.113.4/32 \
  -e ADMIN_DOMAIN=admin.example -e ADMIN_ALLOWED_CIDR=203.0.113.4/32 \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
  && echo "caddyfile OK"
```
Expected: `Valid configuration` + `caddyfile OK`.

- [ ] **Step 3: Run the contract test**

Run: `python -m pytest scripts/tests/test_admin_infra.py -k caddy -v`
Expected: PASS (both the edge block and the inner SPA fallback assertions).

- [ ] **Step 4: Commit**

```bash
git add infra/Caddyfile
git commit -m "feat(infra): Caddy admin.<domain> site — CIDR-gated, same-origin SPA + /v1 proxy"
```

---

### Task 5: Terraform DNS + CI + env docs + runbook

**Files:**
- Modify: `infra/terraform/dns.tf`
- Modify: `.github/workflows/build.yml`
- Modify: `.github/workflows/test.yml`, `.github/workflows/lint.yml`
- Modify: `infra/.env.prod.example`, `infra/.env.example`
- Modify: `infra/README.md`

- [ ] **Step 1: Terraform admin DNS record**

Append to `infra/terraform/dns.tf` (mirror the `grafana` record):
```hcl
resource "cloudflare_dns_record" "admin" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "admin" # -> admin.<zone domain>
  type    = "A"
  content = google_compute_address.usan.address
  proxied = false
  ttl     = 300
}
```
Verify: `cd infra/terraform && terraform fmt -check && terraform validate` (validate
may need `terraform init`; `fmt -check` is the cheap gate). Expected: formatted, valid.

- [ ] **Step 2: build.yml — admin-ui image build**

After the "Build & push agent app" step in the `build` job, add:
```yaml
      - name: Build & push admin-ui
        uses: docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8  # v6
        with:
          context: apps/admin-ui
          file: apps/admin-ui/Dockerfile
          platforms: linux/amd64
          push: true
          tags: |
            ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-admin-ui:${{ steps.tag.outputs.value }}
            ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-admin-ui:${{ github.sha }}
            ${{ env.GAR }}/usan-admin-ui:${{ steps.tag.outputs.value }}
            ${{ env.GAR }}/usan-admin-ui:${{ github.sha }}
          cache-from: type=gha,scope=admin-ui
          cache-to: type=gha,mode=max,scope=admin-ui
```

- [ ] **Step 3: build.yml — deploy job ships the overlay**

In the deploy job's "Copy compose files to VM" SCP `source`, append
`,infra/docker-compose.admin.yml`. In the "Pull images and bring stack up" script,
add `-f infra/docker-compose.admin.yml` as the LAST `-f` in the `COMPOSE=` chain
(after `docker-compose.monitoring.yml`).

- [ ] **Step 4: test.yml — vitest job + pyyaml**

Add a job (pin `actions/setup-node` to its v4 commit SHA — resolve it at
implementation time, e.g. `gh api repos/actions/setup-node/git/refs/tags/v4 -q .object.sha`;
mirror the `# v4` comment style):
```yaml
  vitest-admin-ui:
    name: vitest (apps/admin-ui)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6
      - uses: actions/setup-node@<PINNED_V4_SHA>  # v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: apps/admin-ui/package-lock.json
      - name: Install
        working-directory: apps/admin-ui
        run: npm ci
      - name: vitest
        working-directory: apps/admin-ui
        run: npm test
```
(`pyyaml` already added to `pytest-scripts` in Task 1 Step 2.)

- [ ] **Step 5: lint.yml — eslint + typecheck job**

```yaml
  lint-admin-ui:
    name: Lint admin-ui (apps/admin-ui)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6
      - uses: actions/setup-node@<PINNED_V4_SHA>  # v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: apps/admin-ui/package-lock.json
      - name: Install
        working-directory: apps/admin-ui
        run: npm ci
      - name: eslint
        working-directory: apps/admin-ui
        run: npm run lint
      - name: typecheck
        working-directory: apps/admin-ui
        run: npm run typecheck
```

- [ ] **Step 6: .env.prod.example — admin section**

Append (after the Grafana/observability section):
```bash
# === Admin UI — Google SSO console (Plan admin-5) ===
# The React admin console is reverse-proxied by Caddy at ADMIN_DOMAIN and gated to
# an operator CIDR allowlist at L7 (ADMIN_ALLOWED_CIDR) — same pattern as Grafana.
# Going live needs BOTH a v* tag (ships this overlay) AND `terraform apply` (the
# admin.<domain> DNS record).
ADMIN_DOMAIN=admin.usan.example
# Space-separated CIDR allowlist for the admin subdomain (Caddy remote_ip). Use /32
# for a single host. 203.0.113.0/24 is a DOCUMENTATION range (RFC 5737) — REPLACE
# with your real operator CIDR before deploy or the console is unreachable.
ADMIN_ALLOWED_CIDR=203.0.113.4/32
# Google OAuth 2.0 Web client (GCP console -> APIs & Services -> Credentials).
# The authorized redirect URI MUST be exactly https://${ADMIN_DOMAIN}/v1/auth/callback
# and must be registered on the client (manual GCP step — see infra/README.md).
GOOGLE_OAUTH_CLIENT_ID=__GOOGLE_OAUTH_CLIENT_ID__
GOOGLE_OAUTH_CLIENT_SECRET=__GOOGLE_OAUTH_CLIENT_SECRET__
GOOGLE_OAUTH_REDIRECT_URI=https://admin.usan.example/v1/auth/callback
# Optional G Suite hosted-domain restriction (the `hd` claim). Blank = any Google
# account on the allow-list.
# GOOGLE_OAUTH_HD=usanretirement.com
# Comma-separated emails seeded as role=admin on first boot (the initial allow-list).
# After boot, manage the allow-list in the UI (Admin users screen).
ADMIN_BOOTSTRAP_EMAILS=ops@usanretirement.com
# Session cookie lifetime (seconds) and Secure flag. Keep SESSION_COOKIE_SECURE=true
# in prod (Caddy terminates TLS). Only local http dev sets it false.
# ADMIN_SESSION_TTL_S=28800
SESSION_COOKIE_SECURE=true
# Where /v1/auth/callback sends the browser after login (the SPA root).
# ADMIN_POST_LOGIN_REDIRECT=/
```

- [ ] **Step 7: .env.example (dev) — admin section**

Append:
```bash
# === Admin UI — Google SSO (dev) ===
# The admin SPA (apps/admin-ui) runs via `npm run dev` (Vite, proxies /v1 -> this
# API). SSO is OFF until you set a Google OAuth client. Over local http the session
# cookie cannot be Secure, so set SESSION_COOKIE_SECURE=false for local login.
SESSION_COOKIE_SECURE=false
# GOOGLE_OAUTH_CLIENT_ID=
# GOOGLE_OAUTH_CLIENT_SECRET=
# GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/v1/auth/callback
# ADMIN_BOOTSTRAP_EMAILS=you@example.com
```

- [ ] **Step 8: infra/README.md — admin deploy runbook**

Add a `## Admin UI (Plan admin-5)` section after Monitoring, covering, in order:
1. **Create the Google OAuth Web client** (GCP console → APIs & Services →
   Credentials → OAuth client ID → Web application). Authorized redirect URI =
   `https://admin.<domain>/v1/auth/callback`. Note the client id + secret.
2. `cd infra/terraform && terraform apply` — creates the `admin.<domain>` DNS record.
3. Fold the new keys into the `usan-prod-env` secret: `ADMIN_DOMAIN`,
   `ADMIN_ALLOWED_CIDR`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
   `GOOGLE_OAUTH_REDIRECT_URI`, `ADMIN_BOOTSTRAP_EMAILS` (+ optional
   `GOOGLE_OAUTH_HD`, `SESSION_COOKIE_SECURE=true`).
4. **Seed the VM `.env` BEFORE the tag deploy** — the deploy does not re-fetch the
   secret. Reboot the VM or IAP-SSH and refresh `/opt/usan/infra/.env`, *then* cut
   the tag. (IAP SSH works even if your IP isn't in `operator_ssh_cidr`.)
5. Cut a `v*` tag → the deploy ships `docker-compose.admin.yml`, builds/pulls
   `usan-admin-ui`, and brings up the `admin-ui` service.
6. **Verify:** from an allowlisted IP open `https://admin.<domain>` → SPA → Google
   login → console; from a non-allowlisted IP → **403**; `GET /v1/auth/me` returns
   the operator email.

Also update the manual `docker compose` chain in the README's deploy section to the
full five-file chain (it currently omits monitoring + admin):
`-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml -f docker-compose.monitoring.yml -f docker-compose.admin.yml`.

- [ ] **Step 9: Run the full contract suite**

Run: `python -m pytest scripts/tests/test_admin_infra.py -v`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add infra/terraform/dns.tf .github/workflows/build.yml .github/workflows/test.yml \
  .github/workflows/lint.yml infra/.env.prod.example infra/.env.example infra/README.md
git commit -m "feat(infra,ci): admin DNS, image build + frontend CI, env docs + deploy runbook"
```

---

### Task 6: Full verification

- [ ] **Step 1: Contract + structural** — `python -m pytest scripts/tests -v` → all green (admin + dashboards).
- [ ] **Step 2: Compose + Caddy validation** (commands from Tasks 3/4) → both OK.
- [ ] **Step 3: Frontend toolchain** (the image build runs these, but run directly too):
  `cd apps/admin-ui && npm ci && npm run lint && npm run typecheck && npm test && npm run build` → all green.
- [ ] **Step 4: Image build** — `docker build -t usan-admin-ui:local apps/admin-ui` → OK; serve+fallback smoke (Task 2 Step 4).
- [ ] **Step 5: No source regressions / boundary** — `cd apps/api && uv run pytest -q` (444 pass), `cd services/agent && uv run pytest -q` (152 pass) unaffected; confirm `git diff --name-only origin/admin-ui-p4...HEAD` touches **no** `services/agent/**` file and **no** `apps/api/src/**` file (only compose/CI/docs/admin-ui/scripts).

---

### Task 7: Adversarial review + PR

- [ ] **Step 1:** Run the adversarial multi-agent review workflow over the P5 diff
  (dimensions: compose-merge correctness, Caddy CIDR/same-origin security, CI
  correctness + action-pin hygiene, Docker non-root/static-server security, secret
  handling + deploy-ordering). Skeptic-verify each finding; fix confirmed ones.
- [ ] **Step 2:** Open the stacked PR: `gh pr create --base admin-ui-p4 --head admin-ui-p5`
  with a summary + test plan; PR body ends with the Claude Code attribution line.

## Self-review notes (spec coverage)

- §11 Serving → Task 2 (image) + Task 3 (overlay). §11 Caddy → Task 4. §11 Terraform
  → Task 5.1. §11 CI → Task 5.2/5.3 (+ frontend gate 5.4/5.5). §14 P5 scope (compose,
  Caddyfile, DNS+vars, build.yml job, secret seeding + OAuth docs) → Tasks 3/4/5.
- Deploy sharp edges (§11): new keys seeded before tag deploy, tag **and** apply,
  manual OAuth redirect-URI registration → README runbook (Task 5.8).
- Boundary: zero `services/agent` and zero `apps/api/src` changes (only compose env
  passthroughs touch `apps/api`'s *deployment*, not its code) → Task 6.5 gate.
```
