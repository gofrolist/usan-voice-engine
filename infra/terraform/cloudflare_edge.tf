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
# v5 takes a `config` list attribute (not a top-level `enabled`).
resource "cloudflare_authenticated_origin_pulls" "zone" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  config = [{
    enabled = true
  }]
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
      # Scoped to admin.<domain> (the only host that serves /v1/auth): a burst of
      # 403'd /v1/auth probes on api.<domain> must not consume the same per-IP
      # bucket as real admin logins.
      expression = "(http.host eq \"admin.${data.cloudflare_zone.this[0].name}\" and starts_with(http.request.uri.path, \"/v1/auth\"))"
      enabled    = true
      ratelimit = {
        characteristics     = ["ip.src", "cf.colo.id"]
        period              = 60
        requests_per_period = 30
        mitigation_timeout  = 60
      }
    },
  ]
}

# Cloudflare Access (Zero Trust) for grafana.<domain>. Access apps/policies/IdPs
# are account-scoped in v5. Gated on the account id being set so DNS-only setups
# (or hand-managed Access) stay no-ops.
locals {
  manage_access  = local.manage_dns && var.cloudflare_account_id != ""
  use_google_idp = local.manage_dns && var.cloudflare_account_id != "" && var.cloudflare_access_google_client_id != ""
}

# Google IdP (only when a Google client is provided; otherwise Access uses its
# built-in one-time-PIN email auth).
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

# Allow policy: the operator email allowlist (OR-combined include entries).
resource "cloudflare_zero_trust_access_policy" "grafana_operators" {
  count      = local.manage_access ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "USAN operators"
  decision   = "allow"
  # One OR-combined include entry per operator email (the full list, not just the
  # first — so adding a 2nd operator works, and an empty list can't index-panic).
  include = [
    for addr in var.grafana_access_emails : {
      email = {
        email = addr
      }
    }
  ]
}

# Access application gating grafana.<domain>.
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

output "grafana_access_aud" {
  value = local.manage_access ? cloudflare_zero_trust_access_application.grafana[0].aud : ""
  # Marked sensitive because the app's computed aud carries forward the sensitive
  # mark from its dependency closure (the Google IdP client_secret) under the v5
  # provider. Read it at cutover with `terraform output -raw grafana_access_aud`.
  sensitive   = true
  description = "Cloudflare Access AUD for the Grafana app (set as GF_AUTH_JWT_EXPECT_CLAIMS aud)."
}
