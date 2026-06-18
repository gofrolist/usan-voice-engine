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
