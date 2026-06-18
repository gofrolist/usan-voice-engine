# Tenancy P5 — Public Edge / WAF Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put Cloudflare in front of `admin`/`api`/`grafana`, remove the operator-CIDR gate, gate Grafana with Cloudflare Access, and harden the origin (AOP mTLS, security headers, real client-IP) — so client orgs can reach the admin plane from anywhere, safely.

**Architecture:** Cloudflare proxies the three HTTP planes (DNS `proxied=true`); `lk` stays unproxied for WebRTC/SIP media. The GCP firewall stays world-open on 80/443; **Authenticated Origin Pulls (mTLS)** is what locks the origin so only Cloudflare can reach the HTTP planes. Caddy serves a self-signed cert (`tls internal`) with CF SSL mode "Full"; `lk` keeps Let's Encrypt. The app is unchanged except Caddy now feeds it the real client IP via `CF-Connecting-IP`. All config; no migration, no new secrets.

**Tech Stack:** Caddy 2 (Caddyfile), Docker Compose overlays, Terraform (`cloudflare/cloudflare ~> 5.0`, `hashicorp/google ~> 6.0`), Cloudflare Zero Trust (Access), FastAPI/pytest (one tiny test), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-06-18-tenancy-p5-public-edge-waf-design.md`

**Branch:** `feat/tenancy-p5-public-edge-waf` (already created; the spec is committed on it).

---

## Conventions for this plan

- **Infra "TDD" = validate gates.** Pure config can't run a unit test, so each Caddy/compose/terraform task's "verify" step is a real validator that must pass: `caddy validate`, `docker compose config -q`, `terraform validate`. Treat a non-zero exit exactly like a failing test — fix before moving on.
- **Cloudflare provider v5 is recent and its schema differs from v4.** Where a resource's exact argument names are version-sensitive, the task says so and the `terraform validate` gate is what proves correctness. If validate reports an unknown argument, run `terraform providers schema -json | jq '.provider_schemas'` (or fetch the `cloudflare/cloudflare` v5 docs via Context7) and adjust — this is expected, not a plan defect.
- **Commit after every task** (the branch is squash-merged later, but per-task commits keep review legible).
- **Run app commands from `apps/api`** (`uv run …`). Run `caddy validate` via the pinned `caddy:2-alpine` image so the syntax matches prod.

### Reusable: validate the Caddyfile

Several tasks end with this exact command (env vars give the `{$VAR}` placeholders dummy values; the CA file mount is needed once Task 3 lands):

```bash
# Before Task 3 (no CA file referenced yet):
docker run --rm \
  -e API_DOMAIN=api.example.com -e LIVEKIT_DOMAIN=lk.example.com \
  -e GRAFANA_DOMAIN=grafana.example.com -e ADMIN_DOMAIN=admin.example.com \
  -v "$PWD/infra/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Expected on success: `...valid configuration` and exit 0.

---

## Task 1: Caddy security-headers snippet

**Files:**
- Modify: `infra/Caddyfile` (add a snippet at the top; `import` it into the three proxied sites)

Today the edge sets **no** security headers (verified — no `header` directive anywhere in `infra/Caddyfile`). Opening the edge makes these mandatory.

- [ ] **Step 1: Add the snippet at the very top of `infra/Caddyfile`** (above the `{$API_DOMAIN}` block)

```caddyfile
# Security response headers for the browser-facing planes. Applied to the
# Cloudflare-proxied sites (admin/api/grafana). The CSP is scoped to the admin
# SPA's real origins; if a future asset origin is added, widen it here. Start the
# CSP in Report-Only (Content-Security-Policy-Report-Only) if a console violation
# appears during the cutover verification, then promote to enforcing.
(security_headers) {
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
		Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
		-Server
	}
}
```

- [ ] **Step 2: Import it into the `{$API_DOMAIN}` site** — add `import security_headers` as the first line inside the block (right after `encode zstd gzip`).
- [ ] **Step 3: Import it into the `{$GRAFANA_DOMAIN}` site** — add `import security_headers` right after `encode zstd gzip`.
- [ ] **Step 4: Import it into the `{$ADMIN_DOMAIN}` site** — add `import security_headers` right after `encode zstd gzip`.
- [ ] **Step 5: Verify** — run the Caddyfile validate command (see "Reusable" above). Expected: `valid configuration`, exit 0.
- [ ] **Step 6: Commit**

```bash
git add infra/Caddyfile
git commit -m "feat(infra): add security-headers snippet to the Caddy edge"
```

---

## Task 2: Remove the operator-CIDR gates + use the real client IP

**Files:**
- Modify: `infra/Caddyfile` (the `{$GRAFANA_DOMAIN}` and `{$ADMIN_DOMAIN}` site blocks, plus the `{$API_DOMAIN}` reverse_proxy)

Removes the `@operator remote_ip … respond 403` gate from grafana and admin, and switches the real-client-IP source from `{remote_host}` (which becomes Cloudflare's edge IP once proxied) to `CF-Connecting-IP`.

- [ ] **Step 1: Replace the entire `{$GRAFANA_DOMAIN}` block** with the un-gated version

```caddyfile
{$GRAFANA_DOMAIN} {
	encode zstd gzip
	import security_headers
	# Public reachability via Cloudflare; operator-only access is enforced at the
	# Cloudflare Access (Zero Trust) layer in front of this origin, not a CIDR gate.
	reverse_proxy grafana:3000
}
```

- [ ] **Step 2: Replace the entire `{$ADMIN_DOMAIN}` block** with the un-gated version (note `{remote_host}` → `{http.request.header.Cf-Connecting-Ip}`)

```caddyfile
{$ADMIN_DOMAIN} {
	encode zstd gzip
	import security_headers
	# The admin SPA and the API share THIS origin, so the SameSite=Strict session
	# cookie set by /v1/auth works (no cross-site cookie, no CORS). Reachability is
	# public via Cloudflare; auth is enforced at the app layer (Google SSO + RLS).
	@api path /v1/*
	handle @api {
		reverse_proxy api:8000 {
			# Behind Cloudflare the socket peer is the CF edge; the true client is in
			# CF-Connecting-IP. Feed THAT to the API (rate limiter + PHI audit log).
			header_up X-Forwarded-For {http.request.header.Cf-Connecting-Ip}
		}
	}
	# Everything else is the React SPA static container (client-side routing handled
	# inside that container via try_files -> /index.html).
	handle {
		reverse_proxy admin-ui:8080
	}
}
```

- [ ] **Step 3: Update the `{$API_DOMAIN}` reverse_proxy** — change its `header_up X-Forwarded-For {remote_host}` to `header_up X-Forwarded-For {http.request.header.Cf-Connecting-Ip}`. The surrounding `request_body`, `@metrics`, and `@adminplane` directives stay exactly as they are.

- [ ] **Step 4: Verify** — run the Caddyfile validate command. Expected: `valid configuration`, exit 0. Also grep to confirm no gate remains:

```bash
grep -n "remote_ip\|ALLOWED_CIDR\|respond 403" infra/Caddyfile
```
Expected: the only `respond 403` lines left are the `@metrics` and `@adminplane` ones in the `{$API_DOMAIN}` block; **no `remote_ip` and no `*_ALLOWED_CIDR`**.

- [ ] **Step 5: Commit**

```bash
git add infra/Caddyfile
git commit -m "feat(infra): remove operator-CIDR gate; key real client IP off CF-Connecting-IP"
```

---

## Task 3: Authenticated Origin Pulls + self-signed origin cert

**Files:**
- Create: `infra/cloudflare-origin-pull-ca.pem` (the **public** Cloudflare origin-pull CA cert)
- Modify: `infra/Caddyfile` (the three proxied sites gain a `tls` block)
- Modify: `infra/docker-compose.tls.yml` (mount the CA into the caddy container)

AOP makes "only Cloudflare can reach the origin HTTP planes": Caddy requires Cloudflare's client certificate (mTLS). The serving cert is self-signed (`tls internal`); CF SSL mode "Full" accepts it.

- [ ] **Step 1: Add the Cloudflare origin-pull CA cert** to `infra/cloudflare-origin-pull-ca.pem`. This is Cloudflare's **public, well-known** origin-pull CA (not a secret). Fetch the current PEM from Cloudflare's documented origin-pull CA URL and save it verbatim:

```bash
curl -fsS https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem \
  -o infra/cloudflare-origin-pull-ca.pem
head -1 infra/cloudflare-origin-pull-ca.pem   # expect: -----BEGIN CERTIFICATE-----
```
If that URL 404s, the same PEM is linked from the Cloudflare "Authenticated Origin Pulls" docs (Zone-level → "Cloudflare certificate"). Save the PEM contents to the file.

- [ ] **Step 2: Add a `tls` block to each proxied site** in `infra/Caddyfile`. For `{$API_DOMAIN}`, `{$GRAFANA_DOMAIN}`, and `{$ADMIN_DOMAIN}`, insert this immediately after `encode zstd gzip` / `import security_headers`:

```caddyfile
	# Cloudflare-proxied origin: serve a self-signed cert (CF SSL mode "Full"
	# accepts it) and REQUIRE Cloudflare's Authenticated-Origin-Pulls client cert
	# so only Cloudflare can reach this origin (direct-to-IP hits are rejected).
	tls {
		issuer internal
		client_auth {
			mode require_and_verify
			trust_pool file {
				pem_file /etc/caddy/cloudflare-origin-pull-ca.pem
			}
		}
	}
```

Do **not** add this to the `{$LIVEKIT_DOMAIN}` (`lk`) block — it stays unproxied and keeps automatic Let's Encrypt.

> Caddy syntax note: `caddy:2-alpine` is current 2.x, where client-cert trust uses `trust_pool file { pem_file <path> }`. If validate reports `trust_pool` unknown, the running image is older 2.7.x — use `trusted_ca_cert_file /etc/caddy/cloudflare-origin-pull-ca.pem` inside `client_auth` instead. The validate gate (Step 4) tells you which.

- [ ] **Step 3: Mount the CA into the caddy container** — in `infra/docker-compose.tls.yml`, add to the `caddy.volumes` list (after the `./Caddyfile:...:ro` line):

```yaml
      - ./cloudflare-origin-pull-ca.pem:/etc/caddy/cloudflare-origin-pull-ca.pem:ro
```

- [ ] **Step 4: Verify** — validate the Caddyfile **with the CA file mounted** (so `pem_file` resolves):

```bash
docker run --rm \
  -e API_DOMAIN=api.example.com -e LIVEKIT_DOMAIN=lk.example.com \
  -e GRAFANA_DOMAIN=grafana.example.com -e ADMIN_DOMAIN=admin.example.com \
  -v "$PWD/infra/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$PWD/infra/cloudflare-origin-pull-ca.pem:/etc/caddy/cloudflare-origin-pull-ca.pem:ro" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```
Expected: `valid configuration`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add infra/Caddyfile infra/cloudflare-origin-pull-ca.pem infra/docker-compose.tls.yml
git commit -m "feat(infra): require Cloudflare AOP mTLS + self-signed origin cert on proxied sites"
```

---

## Task 4: Drop the now-unused CIDR env + wire Grafana CF-Access SSO

**Files:**
- Modify: `infra/docker-compose.tls.yml` (remove `GRAFANA_ALLOWED_CIDR`)
- Modify: `infra/docker-compose.admin.yml` (remove `ADMIN_ALLOWED_CIDR`)
- Modify: `infra/docker-compose.monitoring.yml` (add Grafana JWT auth env)

The `*_ALLOWED_CIDR` envs are unused once the gates are gone — and their `:?required` guards will fail the boot if left. Grafana trusts the Cloudflare Access JWT so the edge Google login flows straight in (single sign-on).

- [ ] **Step 1: Remove `GRAFANA_ALLOWED_CIDR`** from `infra/docker-compose.tls.yml` — delete the line `GRAFANA_ALLOWED_CIDR: ${GRAFANA_ALLOWED_CIDR:?GRAFANA_ALLOWED_CIDR must be set to an operator CIDR allowlist}` from the `caddy.environment` map. Leave `API_DOMAIN`, `LIVEKIT_DOMAIN`, `GRAFANA_DOMAIN`.

- [ ] **Step 2: Remove `ADMIN_ALLOWED_CIDR`** from `infra/docker-compose.admin.yml` — delete the line `ADMIN_ALLOWED_CIDR: ${ADMIN_ALLOWED_CIDR:?ADMIN_ALLOWED_CIDR must be set to an operator CIDR allowlist}` from the `caddy.environment` map. Leave `ADMIN_DOMAIN`.

- [ ] **Step 3: Add the Grafana JWT auth env** to the `grafana.environment` map in `infra/docker-compose.monitoring.yml`. These values are **non-secret** (team domain, public JWK URL, Access app AUD), so they live in compose, not `.env`. Replace `usanretirement` / the AUD placeholder with the real Cloudflare team name and the Access app AUD produced by Task 7 (cross-reference Task 7 Step 6):

```yaml
      # Single sign-on via Cloudflare Access: Grafana trusts the signed CF Access
      # JWT (header Cf-Access-Jwt-Assertion), so the edge Google login flows in
      # with no second Grafana login. AUD + team domain come from the Access app
      # (terraform, Task 7). All non-secret. GF_SECURITY_ADMIN_PASSWORD stays as
      # the break-glass local login (reachable only after passing CF Access).
      GF_AUTH_JWT_ENABLED: "true"
      GF_AUTH_JWT_HEADER_NAME: "Cf-Access-Jwt-Assertion"
      GF_AUTH_JWT_EMAIL_CLAIM: "email"
      GF_AUTH_JWT_USERNAME_CLAIM: "email"
      GF_AUTH_JWT_JWK_SET_URL: "https://usanretirement.cloudflareaccess.com/cdn-cgi/access/certs"
      GF_AUTH_JWT_EXPECT_CLAIMS: '{"aud":"REPLACE_WITH_ACCESS_APP_AUD"}'
      GF_AUTH_JWT_AUTO_SIGN_UP: "true"
      GF_AUTH_JWT_ROLE_ATTRIBUTE_STRICT: "false"
```

> If you reach this task before Task 7 has produced the real `<team>` and AUD, leave the two placeholder tokens (`usanretirement`, `REPLACE_WITH_ACCESS_APP_AUD`) and fix them in Task 7 Step 6 — the cutover runbook (Task 11) re-checks them. This is a known forward-reference, not a placeholder gap.

- [ ] **Step 4: Verify** the compose files still parse (dummy env so the no-longer-`:?required` files render):

```bash
cd infra && IMAGE_TAG=dummy API_DOMAIN=a ADMIN_DOMAIN=b GRAFANA_DOMAIN=c LIVEKIT_DOMAIN=d \
  GF_SECURITY_ADMIN_PASSWORD=x GF_POSTGRES_RO_PASSWORD=y GRAFANA_DB_HOST=z:5432 \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
  -f docker-compose.monitoring.yml -f docker-compose.admin.yml config -q && echo OK; cd ..
```
Expected: `OK`, exit 0, and **no** error about `ADMIN_ALLOWED_CIDR`/`GRAFANA_ALLOWED_CIDR` being required.

- [ ] **Step 5: Commit**

```bash
git add infra/docker-compose.tls.yml infra/docker-compose.admin.yml infra/docker-compose.monitoring.yml
git commit -m "feat(infra): drop operator-CIDR env; trust Cloudflare Access JWT for Grafana SSO"
```

---

## Task 5: Flip the DNS records to proxied

**Files:**
- Modify: `infra/terraform/dns.tf` (lines 15–53: the four `cloudflare_dns_record` resources)

Proxy `admin`/`api`/`grafana`; keep `lk` direct. Also update the stale top comment.

- [ ] **Step 1: Set `proxied = true`** on the `api`, `grafana`, and `admin` `cloudflare_dns_record` resources (lines 15–23, 35–43, 45–53). **Leave `lk` (lines 25–33) `proxied = false`.**

- [ ] **Step 2: Update the header comment** (lines 1–5) to reflect P5:

```hcl
# === Cloudflare DNS ===
# admin/api/grafana are PROXIED (orange-cloud) so they sit behind Cloudflare's
# WAF/DDoS/Access (Tenancy P5). lk stays proxied=false: Cloudflare's proxy can't
# pass WebRTC/SIP UDP, and lk uses Caddy's Let's Encrypt cert directly. The
# proxied origins use Authenticated Origin Pulls + a self-signed cert (see
# infra/Caddyfile) so only Cloudflare can reach them.
```

- [ ] **Step 3: Verify**

```bash
cd infra/terraform && terraform fmt -check && terraform init -backend=false -input=false >/dev/null && terraform validate; cd ../..
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/dns.tf
git commit -m "feat(infra): proxy admin/api/grafana through Cloudflare (lk stays direct)"
```

---

## Task 6: Cloudflare edge — SSL mode, AOP, and the Free-plan WAF

**Files:**
- Create: `infra/terraform/cloudflare_edge.tf`

Zone settings (SSL "Full", Always-Use-HTTPS), zone-level Authenticated Origin Pulls, and the WAF rulesets (one custom-rules ruleset, one rate-limit ruleset). Gated on `local.manage_dns` (defined in `dns.tf`) so it no-ops when Cloudflare isn't managed.

- **Webhook SKIP custom rule:** the custom-rules ruleset's first rule is a `skip` (action_parameters `ruleset = "current"`) on `/v1/webhooks/*`, so the Free **managed ruleset** is bypassed for the Telnyx/LiveKit server-to-server webhook surface. Without it, the managed ruleset (and any Managed Challenge) could challenge or block these unattended machine-to-machine POSTs — there is no browser to solve a challenge — silently breaking inbound-call and telephony callbacks. The skip is scoped to the webhook path only; the rest of the surface stays under the managed ruleset.

- [ ] **Step 1: Create `infra/terraform/cloudflare_edge.tf`**

```hcl
# Tenancy P5 — Cloudflare edge config (provider cloudflare/cloudflare ~> 5.0).
# Gated on local.manage_dns (defined in dns.tf). SSL "Full" pairs with Caddy's
# self-signed origin cert; AOP (mTLS) is the real origin lockdown. WAF is the
# Free-plan surface: managed ruleset (auto) + custom rules + 1 rate-limit rule.
# Bot Fight Mode is a Free-tier dashboard toggle (no stable v5 resource) — see
# the cutover runbook.

data "cloudflare_zone" "this" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
}

resource "cloudflare_zone_setting" "ssl" {
  count      = local.manage_dns ? 1 : 0
  zone_id    = var.cloudflare_zone_id
  setting_id = "ssl"
  value      = "full"
}

resource "cloudflare_zone_setting" "always_use_https" {
  count      = local.manage_dns ? 1 : 0
  zone_id    = var.cloudflare_zone_id
  setting_id = "always_use_https"
  value      = "on"
}

# Zone-level Authenticated Origin Pulls: CF presents its client cert to the
# origin on every proxied request. Caddy requires + verifies it (infra/Caddyfile).
resource "cloudflare_authenticated_origin_pulls" "zone" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  enabled = true
}

# Custom WAF rules (Free allows up to 5). Defense-in-depth over the Caddy 403s.
resource "cloudflare_ruleset" "waf_custom" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "usan-waf-custom"
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules = [
    {
      action = "skip"
      action_parameters = {
        ruleset = "current"
      }
      expression  = "(starts_with(http.request.uri.path, \"/v1/webhooks\"))"
      description = "Skip the Free managed ruleset for server-to-server webhooks"
      enabled     = true
    },
    {
      action      = "block"
      expression  = "(http.request.uri.path eq \"/metrics\")"
      description = "Block /metrics at the edge"
      enabled     = true
    },
    {
      action      = "block"
      expression  = "(http.host eq \"api.${data.cloudflare_zone.this[0].name}\" and (starts_with(http.request.uri.path, \"/v1/admin\") or starts_with(http.request.uri.path, \"/v1/auth\")))"
      description = "Admin/auth plane is served only via admin.<domain>"
      enabled     = true
    },
  ]
}

# One rate-limit rule (Free allows 1): throttle the login/SSO surface.
resource "cloudflare_ruleset" "rate_limit" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "usan-rate-limit"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [
    {
      action      = "block"
      description = "Throttle the auth/SSO endpoints per IP"
      expression  = "(starts_with(http.request.uri.path, \"/v1/auth\"))"
      enabled     = true
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 60
        requests_per_period = 30
        mitigation_timeout  = 60
      }
    },
  ]
}
```

- [ ] **Step 2: Verify**

```bash
cd infra/terraform && terraform fmt -check && terraform init -backend=false -input=false >/dev/null && terraform validate; cd ../..
```
Expected: `Success! The configuration is valid.` **If validate flags an unknown argument** (e.g. `rules` block vs attribute, `ratelimit` field names, `cloudflare_zone_setting` shape, or the `cloudflare_zone` data-source output name), reconcile against the v5 provider schema (`terraform providers schema -json` or Context7 `cloudflare/cloudflare` v5 docs) and re-run. This reconciliation IS the task.

- [ ] **Step 3: Commit**

```bash
git add infra/terraform/cloudflare_edge.tf
git commit -m "feat(infra): Cloudflare edge — SSL Full, Authenticated Origin Pulls, Free WAF rules"
```

---

## Task 7: Cloudflare Access (Zero Trust) for Grafana

**Files:**
- Modify: `infra/terraform/variables.tf` (add account-id + Access vars)
- Modify: `infra/terraform/cloudflare_edge.tf` (Access IdP + application + policy + output)

Gate `grafana.<domain>` behind Cloudflare Access with Google login + an email allowlist (mirrors `ADMIN_BOOTSTRAP_EMAILS`). Access apps/policies/IdPs are **account-scoped** in v5.

- [ ] **Step 1: Add the variables** to `infra/terraform/variables.tf`

```hcl
variable "cloudflare_account_id" {
  type        = string
  default     = ""
  description = "Cloudflare account ID (for Zero Trust Access). Empty = skip Access (manage by hand)."
}

variable "grafana_access_emails" {
  type        = list(string)
  default     = ["gmrnsk@gmail.com"]
  description = "Operator emails allowed into Grafana via Cloudflare Access. Keep aligned with ADMIN_BOOTSTRAP_EMAILS."
}

variable "cloudflare_access_google_client_id" {
  type        = string
  default     = ""
  description = "Google OAuth client ID for the Cloudflare Access Google IdP. Empty = fall back to one-time-PIN email auth."
}

variable "cloudflare_access_google_client_secret" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Google OAuth client secret for the Cloudflare Access Google IdP."
}
```

- [ ] **Step 2: Add a `locals` block** to `cloudflare_edge.tf` gating Access on the account id being set

```hcl
locals {
  manage_access  = local.manage_dns && var.cloudflare_account_id != ""
  use_google_idp = local.manage_dns && var.cloudflare_account_id != "" && var.cloudflare_access_google_client_id != ""
}
```

- [ ] **Step 3: Add the Google IdP** (only when a Google client is provided; otherwise Access uses its built-in one-time-PIN)

```hcl
resource "cloudflare_zero_trust_access_identity_provider" "google" {
  count      = local.use_google_idp ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Google"
  type       = "google"
  config = {
    client_id     = var.cloudflare_access_google_client_id
    client_secret = var.cloudflare_access_google_client_secret
  }
}
```

- [ ] **Step 4: Add the Access policy** (allow the operator emails)

```hcl
resource "cloudflare_zero_trust_access_policy" "grafana_operators" {
  count      = local.manage_access ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "USAN operators"
  decision   = "allow"
  include = [
    {
      email = {
        email = var.grafana_access_emails[0]
      }
    },
  ]
}
```

> Single-operator case: the one `include` entry above is complete. To add operators, append one `{ email = { email = "<addr>" } }` object per address (Access `include` is OR-combined).

- [ ] **Step 5: Add the Access application** for grafana

```hcl
resource "cloudflare_zero_trust_access_application" "grafana" {
  count                     = local.manage_access ? 1 : 0
  account_id                = var.cloudflare_account_id
  name                      = "USAN Grafana"
  domain                    = "grafana.${data.cloudflare_zone.this[0].name}"
  type                      = "self_hosted"
  session_duration          = "24h"
  policies                  = [cloudflare_zero_trust_access_policy.grafana_operators[0].id]
  allowed_idps              = local.use_google_idp ? [cloudflare_zero_trust_access_identity_provider.google[0].id] : null
  auto_redirect_to_identity = local.use_google_idp
}
```

- [ ] **Step 6: Add an output** for the AUD (consumed by Grafana's `GF_AUTH_JWT_EXPECT_CLAIMS` in Task 4 Step 3, finalized at cutover Task 11 Step 3)

```hcl
output "grafana_access_aud" {
  value       = local.manage_access ? cloudflare_zero_trust_access_application.grafana[0].aud : ""
  description = "Cloudflare Access AUD for the Grafana app (set as GF_AUTH_JWT_EXPECT_CLAIMS aud)."
}
```

- [ ] **Step 7: Verify**

```bash
cd infra/terraform && terraform fmt -check && terraform init -backend=false -input=false >/dev/null && terraform validate; cd ../..
```
Expected: `Success! The configuration is valid.` Reconcile any v5 schema mismatch as in Task 6 Step 2 (the `cloudflare_zero_trust_access_*` resources are the most version-sensitive — confirm `include`/`config`/`policies`/`allowed_idps`/`aud` shapes against the v5 docs).

- [ ] **Step 8: Commit**

```bash
git add infra/terraform/variables.tf infra/terraform/cloudflare_edge.tf
git commit -m "feat(infra): Cloudflare Access (Google + email allowlist) gating Grafana"
```

---

## Task 8: App — document + test the CF-Connecting-IP real-IP contract

**Files:**
- Modify: `apps/api/src/usan_api/client_ip.py` (docstring only)
- Modify: `apps/api/tests/test_client_ip.py` (one new test)

No behavior change — `client_ip()` already reads the XFF first hop, which is exactly what Caddy now fills from `CF-Connecting-IP`. This task locks that contract with a test and corrects the docstring (which still references the old `{remote_host}` rewrite).

- [ ] **Step 1: Write the test** — add to `apps/api/tests/test_client_ip.py`

```python
def test_client_ip_reads_cloudflare_real_client():
    # Behind Cloudflare, Caddy sets X-Forwarded-For to CF-Connecting-IP (the true
    # external client). client_ip() must surface that, not the Cloudflare edge IP
    # (which would be request.client.host / the proxy peer).
    assert client_ip(_req(xff="198.51.100.23", client=("10.0.0.1", 1234))) == "198.51.100.23"
```

- [ ] **Step 2: Run it to confirm it passes** (it documents existing behavior; the point is the assertion, not red→green)

```bash
cd apps/api && uv run pytest tests/test_client_ip.py -v; cd ../..
```
Expected: all tests in the file PASS, including the new one.

- [ ] **Step 3: Update the `client_ip.py` module docstring** — replace the existing module docstring with:

```python
"""Real client IP extraction, shared by rate limiting and PHI access audit logs.

Behind Caddy the socket peer is the proxy container (and, once Cloudflare-proxied,
the Cloudflare edge), not the operator. Caddy overwrites ``X-Forwarded-For`` with
the true client — ``CF-Connecting-IP`` behind Cloudflare (see ``infra/Caddyfile``:
``header_up X-Forwarded-For {http.request.header.Cf-Connecting-Ip}``) — so its
first hop is the real external client. Both the rate-limit key and the audit trail
must use that, not ``request.client.host`` — otherwise every request collapses
into the single proxy/edge IP.
"""
```

- [ ] **Step 4: Run the test + lint + types**

```bash
cd apps/api && uv run pytest tests/test_client_ip.py -v && uv run ruff check usan_api/client_ip.py && uv run mypy usan_api/client_ip.py; cd ../..
```
Expected: tests PASS, ruff clean, mypy clean. (CI runs mypy — run it locally or CI fails.)

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/client_ip.py apps/api/tests/test_client_ip.py
git commit -m "test(api): lock the CF-Connecting-IP real-client-IP contract; fix docstring"
```

---

## Task 9: CI — validate the Caddyfile + ship the CA on deploy

**Files:**
- Modify: `.github/workflows/build-check.yml` (add a Caddyfile-validate job + path triggers)
- Modify: `.github/workflows/build.yml` (add the CA file to the deploy `scp` source list)

A malformed edge config must fail a PR, not the prod deploy. And the deploy must copy the new CA file to the VM.

> **Terraform is now validated on PRs too.** A `terraform-validate` CI job (`terraform fmt -check` + `terraform init -backend=false` + `terraform validate` over `infra/terraform/`) runs alongside `caddy-validate`, so a malformed `cloudflare_edge.tf` / `dns.tf` / variables change fails the PR rather than surfacing only at `terraform apply` (the cutover, Task 11). This makes the per-task `terraform validate` gates used throughout this plan part of the PR-required checks.

- [ ] **Step 1: Add a `caddy-validate` job** to `.github/workflows/build-check.yml` (a sibling of `build-check`). It validates the prod Caddyfile with the CA mounted and dummy domains:

```yaml
  caddy-validate:
    name: Validate Caddyfile
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6
      - name: caddy validate
        run: |
          docker run --rm \
            -e API_DOMAIN=api.example.com -e LIVEKIT_DOMAIN=lk.example.com \
            -e GRAFANA_DOMAIN=grafana.example.com -e ADMIN_DOMAIN=admin.example.com \
            -v "$PWD/infra/Caddyfile:/etc/caddy/Caddyfile:ro" \
            -v "$PWD/infra/cloudflare-origin-pull-ca.pem:/etc/caddy/cloudflare-origin-pull-ca.pem:ro" \
            caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

- [ ] **Step 2: Add the `paths` trigger** — add `infra/Caddyfile` and `infra/cloudflare-origin-pull-ca.pem` to `build-check.yml`'s `on.pull_request.paths` list so the job runs when the edge config changes.

- [ ] **Step 3: Add the CA to the deploy `scp` source** — in `.github/workflows/build.yml`, the "Copy compose files to VM" step's `source:` string (currently ending `…,infra/provision-sip-inbound.sh`) must also include `infra/cloudflare-origin-pull-ca.pem`. Append `,infra/cloudflare-origin-pull-ca.pem` to that comma-separated list.

- [ ] **Step 4: Verify** the workflow YAML is well-formed

```bash
python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['.github/workflows/build-check.yml','.github/workflows/build.yml']]; print('yaml ok')"
```
Expected: `yaml ok`. (If `actionlint` is installed, also run `actionlint .github/workflows/build-check.yml .github/workflows/build.yml`.)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/build-check.yml .github/workflows/build.yml
git commit -m "ci: validate the Caddyfile on PRs; ship the Cloudflare AOP CA on deploy"
```

---

## Task 10: Docs — refresh `.env.prod.example`

**Files:**
- Modify: `infra/.env.prod.example` (remove `GRAFANA_ALLOWED_CIDR` + `ADMIN_ALLOWED_CIDR`; update the surrounding comments)

The `*_ALLOWED_CIDR` keys are gone from compose; the example must not advertise them as required.

- [ ] **Step 1: Remove `GRAFANA_ALLOWED_CIDR`** (the `GRAFANA_ALLOWED_CIDR=...` line and its 2–3 explanatory comment lines, ~`infra/.env.prod.example:157–161`). Replace the Grafana access comment (~lines 154–155) with:

```bash
# Grafana is reverse-proxied by Caddy at GRAFANA_DOMAIN, proxied through Cloudflare,
# and gated to operators via Cloudflare Access (Zero Trust) — no CIDR allowlist.
```

- [ ] **Step 2: Remove `ADMIN_ALLOWED_CIDR`** (the `ADMIN_ALLOWED_CIDR=...` line, ~line 182) and update the admin comment (~lines 173–174) to:

```bash
# The React admin console is reverse-proxied by Caddy at ADMIN_DOMAIN and proxied
# through Cloudflare (WAF/DDoS). Client orgs reach it publicly; access is enforced
# at the app layer (Google SSO + RLS). No CIDR allowlist.
```

- [ ] **Step 3: Verify** the keys are gone

```bash
grep -n "ALLOWED_CIDR" infra/.env.prod.example || echo "no ALLOWED_CIDR keys remain"
```
Expected: `no ALLOWED_CIDR keys remain`.

- [ ] **Step 4: Commit**

```bash
git add infra/.env.prod.example
git commit -m "docs(infra): drop *_ALLOWED_CIDR from the env example (gate removed in P5)"
```

---

## Task 11: Cutover + operator verification (manual — operator-gated)

**Files:** none (operational runbook; record results in the PR description)

This is the one manual task (analogous to P4's Task C2). It needs Cloudflare account access + a `terraform apply` + a `v*` tag deploy, so it cannot be done autonomously. The build tasks above are complete and merged before this runs.

- [ ] **Step 1: One-time Cloudflare account prerequisites**
  - In the GCP console, create a Google OAuth client (Web application) with the authorized redirect URI `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`; capture client id/secret into `cloudflare_access_google_client_id` / `cloudflare_access_google_client_secret` (terraform tfvars). *(Skip to use one-time-PIN email auth instead of Google.)*
  - Set `cloudflare_account_id` in tfvars.
  - Cloudflare dashboard → Security → Bots: **leave Bot Fight Mode OFF by default.** On the Free plan Bot Fight Mode is a global toggle that **cannot be path-exempted** (unlike the managed-ruleset SKIP rule for `/v1/webhooks/*`), so it can challenge or block the Telnyx/LiveKit server-to-server webhooks — there is no browser to solve a challenge — silently breaking inbound calls and telephony callbacks. It may be enabled later **ONLY after** verifying that a real webhook survives it (re-run the Task 11 Step 5 webhook cutover check with Bot Fight Mode on before leaving it on).

- [ ] **Step 2: `terraform plan` then `apply`** in the planned window. Confirm the plan touches only Cloudflare (SSL/AOP/WAF/Access + DNS `proxied`). `apply`. Then capture `terraform output -raw grafana_access_aud` and the team domain.

- [ ] **Step 3: Finish the Grafana JWT wiring** — put the real `<team>` and AUD into `infra/docker-compose.monitoring.yml` (Task 4 Step 3), commit on the branch, include it in the deploy tag.

- [ ] **Step 4: Deploy the edge — cut a `v*` tag.** The deploy ships the new Caddyfile + CA + compose and restarts Caddy/Grafana. (Per the deploy model: terraform apply moved Cloudflare; the tag moves the VM. Brief admin/grafana unavailability between the proxied flip and the tag deploy is expected — keep the window short.)

- [ ] **Step 5: Verify (acceptance checklist):**
  1. `curl -sI https://admin.usanretirement.com` → headers include `server: cloudflare` and `cf-ray`.
  2. Direct-to-origin refused: `curl -k --resolve admin.usanretirement.com:443:<VM_IP> https://admin.usanretirement.com/` → TLS handshake fails (AOP requires CF's client cert).
  3. Security headers present on an admin response: `Strict-Transport-Security`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy`.
  4. Log into admin as `gmrnsk@gmail.com`; perform an action; confirm the PHI-access audit row shows the **real** client IP, not a Cloudflare IP.
  5. `curl https://api.usanretirement.com/metrics` → blocked/403 at the edge.
  6. Visit `https://grafana.usanretirement.com` → Cloudflare Access Google login; `gmrnsk@gmail.com` admitted and Grafana opens **without** a second login (JWT SSO); a non-allowlisted email is denied.
  7. `lk` + a live inbound and outbound call work (media unaffected).
  8. **Webhook cutover check (required gate):** a real inbound call's Telnyx webhook (`/v1/webhooks/*`) completes end-to-end through Cloudflare — i.e. the call's outcome is recorded — confirming the managed-ruleset SKIP rule lets the server-to-server webhook through (and that Bot Fight Mode, if ever turned on, has not started challenging it).
  9. Admin browser console shows **no CSP violations** — and you MUST exercise the cross-origin paths the report-only policy is widened for, otherwise a clean console is meaningless: open the Editor and **run a browser test call** (TestAudioPanel → "Start test call", which `room.connect`s to `wss://lk.<domain>`, a different origin governed by `connect-src`) and play a recording in RecordingPlayer (cross-origin GCS `media-src`). Only after these surface no violations is it safe to switch the CSP `header` from `Content-Security-Policy-Report-Only` to the enforcing `Content-Security-Policy` (Task 1). If a violation appears, widen the offending directive in the Task 1 snippet, redeploy, re-verify, then enforce.

- [ ] **Step 6: Rollback (only if needed):** `terraform apply` with the `proxied` flags set back to `false` (revert Task 5) + re-deploy the prior tag → back to the CIDR-gated, direct-TLS state. Config-only; no data involved.

---

## Self-Review (completed by the plan author)

**1. Spec coverage** — every spec §6 change maps to a task: Cloudflare DNS proxied (Task 5), SSL/AOP/WAF (Task 6), CF Access (Task 7), Caddy gate-removal/real-IP/headers/AOP-cert (Tasks 1–3), compose env-drop + Grafana JWT (Task 4), CA delivery + Caddyfile CI (Task 9), `.env.prod.example` (Task 10), app real-IP test/docstring (Task 8), cutover runbook + verification (Task 11). Spec §7 (Free-WAF honesty) and §8 (security gaps) are realized across Tasks 1/3/6/7. §9 deploy/cutover = Task 11. §10 testing = the validate gates + Task 8 + Task 11 checklist.

**2. Placeholder scan** — no TBD/TODO/"handle errors". The only deferred values are the Cloudflare **team domain** and Access **AUD**, which do not exist until `terraform apply` creates the Access app; they're handled by the explicit forward-reference (Task 4 Step 3 ↔ Task 7 Step 6 ↔ Task 11 Step 3) with a terraform output to read them. That's an ordering fact, not a missing detail.

**3. Type/name consistency** — `local.manage_dns` (dns.tf) reused; `local.manage_access`/`use_google_idp` defined in Task 7 and used consistently; `data.cloudflare_zone.this[0].name` defined in Task 6 and reused in Task 7; `header_up X-Forwarded-For {http.request.header.Cf-Connecting-Ip}` identical in the api site and admin `@api` handle; the CA path `/etc/caddy/cloudflare-origin-pull-ca.pem` identical in the Caddyfile, the tls.yml mount, and the CI validate; `cloudflare_zone` data source declared once (Task 6) — Task 7 must NOT redeclare it.

**Known risk carried into execution:** the Cloudflare provider v5 resource schemas (rulesets, zero_trust_access_*) are the only places exact argument names may need reconciling — every such task gates on `terraform validate` and says so explicitly.
