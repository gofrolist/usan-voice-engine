# Tenancy P5 — Public Edge / WAF (Cloudflare-proxied) — Design

**Date:** 2026-06-18
**Phase:** P5 of the multi-tenancy roadmap (`docs/superpowers/specs/2026-06-16-tenancy-foundation-design.md` and successors).
**Status:** Design approved; ready for implementation plan.

## 1. Goal

Make the admin plane reachable by client organizations from anywhere on the public
internet, **safely**, by:

1. **Removing the operator-CIDR gate** that currently restricts who can reach
   `admin.<domain>` and `grafana.<domain>` (the only thing keeping the admin plane
   non-public today), and
2. **Putting Cloudflare in front** of the HTTP planes to provide TLS at the edge,
   always-on DDoS mitigation, and a WAF, plus
3. **Gating the operator-only Grafana** behind **Cloudflare Access** (free Zero Trust)
   so it stays operator-only even though its CIDR gate is gone.

This is the activation step that turns P1–P4 (RLS, org identity/RBAC, invitations,
client portal — all merged + deployed, prod on `v0.7.1`) into something a real client
org can actually log into.

## 2. Non-goals (explicitly out of scope for P5)

- **Per-org client dashboards** (fly.io-style per-tenant metrics). Deferred to its own
  future phase; the recommendation is to build them **into the admin UI** (RLS-scoped),
  **not** to expose a multi-tenant Grafana to clients. See the future-work note.
- **GCP Cloud Armor / a Global HTTPS Load Balancer.** Rejected: requires re-architecting
  the single-VM + Caddy-terminates-TLS + SNI model.
- **OWASP Core Ruleset.** Not available on the Cloudflare Free plan (Business+). The Free
  WAF is what we ship; CRS is a later config-only upgrade.
- **Changing the SSH gate.** `operator_ssh_cidr` (port 22) is unrelated to the HTTP edge
  and stays locked.
- **Changing the call/runtime plane.** SIP/RTP/LiveKit ingress is untouched; the runtime
  plane stays single-org (deferred, as in P2–P4).
- **App behavioral changes.** No FastAPI route/auth logic changes are required; the work
  is in Caddy + Cloudflare + compose config.

## 3. Decisions locked during brainstorming (2026-06-18)

| Decision | Choice | Rationale |
|---|---|---|
| WAF direction | **Cloudflare-proxied** | Cloudflare already manages the DNS via terraform (`infra/terraform/dns.tf`, records currently `proxied=false`); flipping to proxied is a known lever, not a new vendor. |
| Proxy scope | **Proxy `admin`, `api`, `grafana`; leave `lk` unproxied** | `lk.<domain>` fronts LiveKit WebRTC/SIP UDP, which the CF proxy cannot carry. |
| Gate removal | **Remove the operator-CIDR gate from BOTH `admin` and `grafana`** | "Open everything." Grafana's gate is replaced by Cloudflare Access (stronger). |
| Cloudflare plan | **Free** | Designed to what Free delivers (§7); origin hardening carries the L7 weight. |
| PHI through Cloudflare | **Accepted "for now"** | The user explicitly accepted plaintext PHI transiting Cloudflare for now. **Hard pre-GA gate:** a Cloudflare BAA (Enterprise) MUST be executed before real PHI volume / general availability. Recorded as the top risk (§11). |
| Grafana operator auth | **Cloudflare Access (free Zero Trust) + email allowlist** | Operators authenticate with their existing Google identity (e.g. `gmrnsk@gmail.com`, a personal Gmail). Grafana's native Google OAuth can only restrict by *domain* (`allowed_domains`), which is useless for `@gmail.com`; CF Access restricts by *exact email* (mirrors `ADMIN_BOOTSTRAP_EMAILS`). |
| Origin lockdown | **Cloudflare Authenticated Origin Pulls (AOP, mTLS)** — not firewall IP-pinning | AOP makes "only Cloudflare can reach the origin HTTP planes" without disturbing `lk`'s direct 443 or chasing Cloudflare's changing IP-range list in the GCP firewall. |
| Origin serving cert | **Caddy `tls internal` (self-signed) + CF SSL mode "Full"** for proxied hosts | Zero cert-delivery friction (no Origin CA key to ship). AOP provides the strong origin-auth. **Upgrade path** (with the BAA/GA work): CF Origin CA cert + SSL "Full (Strict)". |

## 4. Current state (verified against the live files)

- **The "operator-CIDR gate" is L7-only, inside Caddy** (`infra/Caddyfile`):
  - `grafana.<domain>`: `@operator remote_ip {$GRAFANA_ALLOWED_CIDR}` → `handle @operator { reverse_proxy grafana:3000 }` else `respond 403` (Caddyfile:47–57).
  - `admin.<domain>`: `@operator remote_ip {$ADMIN_ALLOWED_CIDR}` → inner `@api`/SPA handles else `respond 403` (Caddyfile:59–82). This origin serves BOTH the React SPA (`admin-ui:8080`) and same-origin `/v1/*` (`api:8000`).
  - Prod values: both set to a single operator host `/32`.
- **The GCP firewall already opens 80/443 to `0.0.0.0/0`** (`infra/terraform/main.tf:113–127`, `usan-allow-web`), so removing the gate needs **no firewall change**. SSH (22) stays `operator_ssh_cidr`; media (UDP 10000–20000 / 50000–60000) stays `0.0.0.0/0`; SIP (5060) stays Telnyx CIDRs.
- **`api.<domain>` is already public and token-only**, hardened with a 1 MB body cap (Caddyfile:8–10), `/metrics`→403 (Caddyfile:19–20), and `/v1/admin/*`+`/v1/auth/*`→403 (Caddyfile:29–30) so the admin plane has a single gated entry point.
- **Cloudflare already manages 4 DNS records** (`api`, `lk`, `grafana`, `admin` — all A → the VM static IP, all `proxied=false`) via the `cloudflare` terraform provider (`dns.tf`, with `var.cloudflare_api_token` + `var.cloudflare_zone_id`).
- **No WAF / Cloud Armor / GCLB / CF proxy exists today.** Edge is stock `caddy:2-alpine` (`infra/docker-compose.tls.yml:9`), Caddyfile mounted read-only, ACME (Let's Encrypt) certs in the `caddy_data` volume.
- **No security response headers** are set at the edge (no HSTS/CSP/X-Frame-Options/X-Content-Type-Options). **No CORS middleware** in the app (works only because admin SPA + `/v1` are same-origin behind the gate).
- **App client-IP derivation:** `usan_api.ratelimit` keys on `client_ip(request)`, which reads `X-Forwarded-For`'s first hop; Caddy currently rewrites XFF to the direct peer (`header_up X-Forwarded-For {remote_host}`, Caddyfile:35,72). So the app needs **no change** as long as Caddy puts the *real* client IP into XFF.
- **Grafana** (`infra/docker-compose.monitoring.yml`): `grafana/grafana:12.4.4`, `GF_AUTH_ANONYMOUS_ENABLED=false`, `GF_USERS_ALLOW_SIGN_UP=false`, only a local `GF_SECURITY_ADMIN_PASSWORD`; no Google/OAuth/JWT auth; reachable only via Caddy (no published host port). Has a read-only Postgres datasource provisioned (`GF_POSTGRES_RO_PASSWORD`).
- **Admin OAuth identity model** (`apps/api/src/usan_api/settings.py`, `oauth.py`, `main.py`): `ADMIN_BOOTSTRAP_EMAILS` (explicit, comma-separated, lowercased email allowlist seeded as super-admins) + invitations for clients; `GOOGLE_OAUTH_HD` is an *optional* hosted-domain pin (oauth.py:63–64,121). So operators are an explicit **email allowlist**, not a Workspace domain.

## 5. Architecture

```
                            ┌──────────────────────────────────────────────┐
client (browser) ─ HTTPS ─▶ │ Cloudflare edge                               │
                            │  • WAF (Free managed ruleset, bot fight,      │
                            │    1 rate-limit rule, custom rules)           │
                            │  • DDoS mitigation (all plans)                │
                            │  • CF Access (Zero Trust) — grafana ONLY      │
                            │  • CF-Connecting-IP: <real client>            │
                            └───────────────┬──────────────────────────────┘
                                            │ AOP (mTLS): CF presents client cert
                                            ▼
                            GCP VM :443 ── Caddy ── per-site:
                              admin.<domain>  → admin-ui:8080  +  api:8000 (/v1/*)   [app auth: Google SSO + RLS]
                              api.<domain>    → api:8000                              [token/webhook auth]
                              grafana.<domain>→ grafana:3000                          [CF Access gate + Grafana]
                            (all three: tls internal, AOP require, real-IP from CF-Connecting-IP)

lk.<domain>  ── HTTPS (UNPROXIED, Let's Encrypt) ──▶ Caddy → host.docker.internal:7880   (LiveKit's own auth)
Telnyx SIP/RTP ── UDP (UNPROXIED, firewall-pinned) ──▶ livekit-sip / livekit              (unchanged)
```

Two ingress classes after P5: **proxied HTTP** (admin/api/grafana, behind Cloudflare + AOP) and **direct** (lk HTTPS + SIP/RTP UDP, untouched). The GCP firewall stays world-open on 80/443; **AOP** — not a firewall IP allowlist — is what stops attackers bypassing Cloudflare straight to the origin HTTP planes (any direct hit with SNI=admin/api/grafana is rejected by Caddy for lacking the CF client cert; a direct hit to lk reaches LiveKit, which has its own auth).

## 6. Components & changes

### 6.1 Cloudflare config — `infra/terraform/` (cloudflare provider, extends `dns.tf`)

- **DNS:** flip `admin`, `api`, `grafana` records to `proxied = true`; **leave `lk` `proxied = false`**.
- **SSL/TLS:** zone SSL mode → **Full** (encrypted CF↔origin; the self-signed origin cert is accepted). *Upgrade later to Full (Strict) with a CF Origin CA cert.*
- **Authenticated Origin Pulls (AOP):** enable zone-level AOP so Cloudflare presents its client certificate to the origin on every proxied request.
- **WAF (Free plan reality):**
  - Free Managed Ruleset — applied automatically.
  - Bot Fight Mode — **on**.
  - **1 rate-limiting rule** — on `/v1/auth/*` (login-flood protection), the highest-value single rule.
  - **Custom WAF rules** (≤5 on Free) — proposed: (1) block `/metrics` at the edge (defense-in-depth over the existing Caddy 403), (2) a basic "block obvious injection / known-bad path" rule; leave headroom for tuning.
- **Cloudflare Access (Zero Trust) for `grafana.<domain>`:**
  - An Access **application** scoped to `grafana.<domain>`.
  - An Access **policy** = **allow emails** in the operator allowlist (initially `gmrnsk@gmail.com`; kept aligned with `ADMIN_BOOTSTRAP_EMAILS`).
  - Identity: Google login (or one-time-PIN to the email).
- **Management:** all of the above as `cloudflare_*` terraform resources (consistent with the existing `dns.tf`), shipped via `terraform apply`. The Free tier supports DNS proxy, AOP, Bot Fight, 1 rate-limit rule, ≤5 custom rules, and Zero Trust Access (≤50 users).

### 6.2 Caddy edge — `infra/Caddyfile`

- **Remove both `@operator remote_ip … respond 403` blocks** (admin + grafana). The `admin.<domain>` site collapses to its inner `@api`(→`api:8000`) / SPA(→`admin-ui:8080`) handles for all source IPs; `grafana.<domain>` becomes a plain `reverse_proxy grafana:3000` (Cloudflare Access now does the gating).
- **Real client IP:** on the proxied sites, rewrite `header_up X-Forwarded-For {http.request.header.Cf-Connecting-Ip}` so the app rate-limiter and audit logs key on the true caller, not the Cloudflare edge IP. Set Caddy `servers.trusted_proxies` to Cloudflare's ranges (via the global options block) so the rewrite is not spoofable; combined with AOP, only Cloudflare can present these requests anyway.
- **AOP (origin lockdown):** per proxied site, `tls { client_auth { mode require_and_verify; trust_pool file <cloudflare-origin-pull-ca.pem> } }`. The Cloudflare origin-pull CA is **public** → committed to `infra/` and scp'd by the deploy.
- **Serving cert:** proxied sites use `tls internal` (Caddy self-signed; CF "Full" accepts it). `lk.<domain>` keeps **Let's Encrypt** (it is direct, so ACME HTTP-01/TLS-ALPN still works; the firewall stays open so the challenge is reachable).
- **Security headers** (new, on the proxied sites): `Strict-Transport-Security`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and a **Content-Security-Policy** for the admin SPA — validated against the SPA's real asset/script/style/connect origins before enabling (see open question OQ1).
- **Keep** the 1 MB body cap and the `api.<domain>` `/metrics`/`/v1/admin/*`/`/v1/auth/*` 403s (defense-in-depth).

### 6.3 Compose — `infra/docker-compose.tls.yml` + `infra/docker-compose.monitoring.yml`

- **Drop the `GRAFANA_ALLOWED_CIDR` env** (incl. the `:?GRAFANA_ALLOWED_CIDR must be set…` required-guard at `docker-compose.tls.yml:19`) and `ADMIN_ALLOWED_CIDR`; both become unused once the matchers are gone. Removing a `:?required` guard is necessary or the stack fails to boot.
- **Mount the Cloudflare origin-pull CA cert** into the Caddy container (read-only) for AOP `trust_pool`.
- **Grafana single-sign-on behind CF Access:** configure Grafana to trust the Cloudflare Access JWT so the edge Google login flows straight through (no second Grafana login):
  - `GF_AUTH_JWT_ENABLED=true`, `GF_AUTH_JWT_HEADER_NAME=Cf-Access-Jwt-Assertion`, `GF_AUTH_JWT_JWK_SET_URL=https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, `GF_AUTH_JWT_EXPECT_CLAIMS={"aud":"<access-app-aud>"}`, `GF_AUTH_JWT_USERNAME_CLAIM=email`, `GF_AUTH_JWT_EMAIL_CLAIM=email`, `GF_AUTH_JWT_AUTO_SIGN_UP=true`, default role Viewer (or Admin) per operator need.
  - These values are **non-secret** (team domain + Access app AUD + a public JWK URL) → put them **directly in the compose env**, not `.env`, to avoid a `.env` refresh. Keep `GF_SECURITY_ADMIN_PASSWORD` as a break-glass local login behind CF Access.

### 6.4 No new secrets, no new required `.env` keys

The chosen design needs **no Origin CA private key** (uses `tls internal`) and puts the Grafana-JWT/CF-Access config in compose (non-secret). So P5 deploys **without** the `.env`-refresh gotcha (`deploy_env_not_refreshed`). The only out-of-band artifact is the **public** Cloudflare origin-pull CA cert, committed to the repo.

## 7. WAF reality on the Free plan (honest scope)

**You get:** always-on **DDoS mitigation** (L3/4 + L7), the **Free Managed Ruleset** (a thin, high-signal set), **Bot Fight Mode**, **1 rate-limiting rule**, **≤5 custom WAF rules**, **Managed Challenge**, and **Zero Trust Access** (≤50 users). **You do NOT get** the OWASP Core Ruleset (Business+). Net: the WAF is real for DDoS/bots/basic rules, but **signature-based L7 attack filtering is limited**, so the origin hardening (§6.2: AOP, security headers, the existing app rate-limiter on the real IP, body caps) is **load-bearing**. Upgrading to Business later (config-only) unlocks CRS + flexible rate limiting.

## 8. Security gaps closed alongside gate-removal

Opening the edge removes the outer fail-closed wrapper, so these ship **in the same change set**:
- **Security headers** (none exist today) — HSTS/CSP/X-Frame/X-Content-Type/Referrer.
- **AOP origin-lockdown** — only Cloudflare can reach the origin HTTP planes.
- **Real-IP attribution** — rate-limit + audit log on the true caller, not the CF edge IP.
- **Edge rate-limiting / bot-fight / DDoS** — volumetric and login-flood defense the single VM cannot provide itself.
- **Grafana no longer relies on a CIDR gate** — Cloudflare Access (email allowlist) replaces it; the monitoring console is never exposed unauthenticated.
- **Leaked-session containment** — the admin session cookie becomes globally replayable once the gate is gone; existing mitigations remain (per-request status + membership re-read revocation, `SESSION_COOKIE_SECURE=true`, `SameSite=Strict`); presigned recording URLs keep their 10-min TTL. (No code change; documented as the standing response.)

## 9. Deploy path & lockout-safe cutover

Both deploy levers are needed (the 3-path model: merging to main alone changes nothing on the VM):
- **`terraform apply`** — Cloudflare resources (DNS proxied flips, SSL mode, AOP, WAF rules, CF Access app/policy).
- **`v* tag`** — Caddy/compose changes (gate removal, AOP `client_auth`, real-IP, security headers, Grafana JWT env, CA cert mount).

The risk is that **gates 403 everyone the moment traffic is proxied** (the `remote_ip` matcher would see CF IPs) and that **AOP rejects everyone while traffic is still direct** (no CF client cert). Because we are *removing* the gates entirely, the clean ordering is:

1. **Pre-stage (terraform, inert while `proxied=false`):** create the WAF rules, AOP setting, CF Access app/policy, set SSL mode = **Full**. None affect direct (unproxied) traffic.
2. **Pre-stage (v\* tag, safe while still gated/unproxied):** add security headers only. (Do NOT enable AOP-require or switch the serving cert yet — those break direct access.)
3. **Cutover (tight, planned, operator-only window):**
   a. `terraform apply` → set `admin`/`api`/`grafana` `proxied=true`. Cloudflare now fronts them; Caddy still serves its valid LE cert (CF "Full" accepts); but the **gates now 403** (they see CF IPs) → brief admin/grafana unavailability (expected).
   b. `v* tag` deploy of the cutover Caddy/compose: **remove the gates**, enable **AOP require**, switch proxied sites to `tls internal`, add the **real-IP** rewrite, mount the CF CA, enable **Grafana JWT**. Now: no gate to 403; AOP satisfied (CF presents its cert); real client IP flows; Grafana SSO via CF Access.
4. **Verify** (operator runbook, §10).

**Rollback:** `terraform apply` to set `proxied=false` + revert the Caddy/compose tag → back to the CIDR-gated, direct-TLS state. (Because no migration and no secret change, rollback is config-only.)

## 10. Testing

Infra-heavy, so a mix of automated validation + a manual operator runbook:

- **Caddyfile validation in CI** — a new check that runs `caddy validate --config infra/Caddyfile --adapter caddyfile` (in a `caddy` container) so a malformed edge config fails the PR, not the deploy.
- **Terraform** — `terraform fmt -check` + `terraform validate` for the new `cloudflare_*` resources (already part of the infra workflow if present; otherwise added).
- **App unit test** — assert `usan_api.client_ip.client_ip()` resolves the caller correctly from the rewritten `X-Forwarded-For` (guards the real-IP contract the rate-limiter depends on). No behavioral app change otherwise; the full backend + admin-ui suites must stay green.
- **Operator verification runbook** (analogous to P4 Task C2 — manual, post-cutover):
  1. `curl -sI https://admin.<domain>` shows `server: cloudflare` + a `cf-ray` header.
  2. Direct-to-origin is refused: `curl --resolve admin.<domain>:443:<VM_IP> https://admin.<domain>` fails the TLS/mTLS handshake (AOP working).
  3. Security headers present on admin responses (HSTS, X-Frame-Options, CSP, etc.).
  4. An audit-log entry after an admin action shows the **real** client IP, not a Cloudflare IP.
  5. A benign attack string (e.g. an obvious SQLi/XSS probe) to a WAF-covered path is challenged/blocked.
  6. `grafana.<domain>` presents the **Cloudflare Access** Google login; `gmrnsk@gmail.com` gets in, a non-allowlisted email does not; Grafana opens without a second login (JWT SSO).
  7. `lk.<domain>` + a live inbound/outbound call are unaffected.

## 11. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **PHI transits Cloudflare without a BAA** (accepted "for now") | High (compliance) | Documented accepted risk; **hard pre-GA gate**: execute a Cloudflare Enterprise BAA before real PHI volume / GA. Until then, limit to test/low-PHI usage. |
| **Cutover lockout / downtime** | Medium | The ordered runbook (§9) + config-only rollback; perform in a planned operator-only window. |
| **CSP breaks the admin SPA** | Medium | Validate the CSP against the SPA's real asset/connect origins before enabling Strict; ship `Content-Security-Policy-Report-Only` first if needed (OQ1). |
| **Free WAF is thin (no CRS)** | Medium | Origin hardening carries the L7 weight (§8); Business upgrade is a config-only path to CRS. |
| **Grafana JWT/CF-Access wiring is the one novel integration** | Low/Medium | Fallback: ship CF Access gating with Grafana's local admin login behind it (double login) if JWT SSO needs more tuning; it does not block the security goal. |
| **AOP + open firewall leaves `lk`→LiveKit as the one un-AOP origin surface** | Low | LiveKit enforces its own API-key/JWT auth on 7880; acceptable. Revisit if `lk` ever serves browser clients. |
| **Zone-level AOP uses Cloudflare's SHARED origin-pull certificate** — it proves "*a* Cloudflare connection," not "*our* zone." A determined attacker routing through their own Cloudflare account to our origin IP could pass AOP and forge `CF-Connecting-IP`, poisoning the rate-limiter / PHI-audit client IP. (Flagged by the automated commit security review on the `CF-Connecting-IP` rewrite.) | Medium | **Bounded impact: no auth bypass and no PHI read** — the app still requires Google SSO + a valid session/token; the exposure is limited to rate-limit evasion + audit-log client-IP integrity. The Caddyfile gates the `CF-Connecting-IP` trust on the adjacent `tls client_auth require_and_verify` (AOP), which already rejects all *non-Cloudflare* peers. Hardening follow-up (with the BAA/GA work): **per-hostname AOP with a custom certificate** binds the origin to our specific zone, closing the shared-cert gap. |

## 12. Open questions (to resolve during implementation)

- **OQ1 — exact CSP:** enumerate the admin SPA's real origins (self, fonts/CDN if any, the same-origin `/v1` connect) and finalize the `Content-Security-Policy`; start in report-only if uncertain.
- **OQ2 — `lk.<domain>:443` external consumers:** confirm nothing external depends on `lk` HTTPS (PSTN-only telephony suggests none); informs whether a future firewall lockdown is even relevant. AOP makes this non-blocking for P5.
- **OQ3 — Cloudflare team domain / Access AUD:** the `<team>.cloudflareaccess.com` domain and the Access app AUD are produced when the Access app is created (terraform) — wire them into the Grafana JWT env in the same change.
- **OQ4 — serving-cert upgrade:** confirm whether to ship Full (Strict) + CF Origin CA cert now or defer to the BAA/GA hardening pass (default: defer; ship `tls internal` + Full).

## 13. Summary of files touched (for the plan)

- `infra/terraform/dns.tf` — `proxied=true` for admin/api/grafana.
- `infra/terraform/*.tf` (new file, e.g. `cloudflare_edge.tf`) — SSL mode, AOP, WAF rules, CF Access app + policy.
- `infra/Caddyfile` — remove gates; AOP `client_auth`; `tls internal`; real-IP; security headers.
- `infra/docker-compose.tls.yml` — drop `GRAFANA_ALLOWED_CIDR`/`ADMIN_ALLOWED_CIDR`; mount the CF origin-pull CA cert.
- `infra/docker-compose.monitoring.yml` — Grafana JWT (CF Access SSO) env.
- `infra/cloudflare-origin-pull-ca.pem` (new) — the public Cloudflare origin-pull CA, scp'd by the deploy (`build.yml` `scp` source list updated).
- `.github/workflows/*` — add the `caddy validate` PR check; add the new CA file to the deploy `scp` source list.
- `apps/api/tests/...` — `client_ip()` real-IP unit test (if not already covered).
- `infra/.env.prod.example` — remove the now-unused `*_ALLOWED_CIDR` examples; document the CF/Access values.
