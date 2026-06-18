# === Cloudflare DNS ===
# admin/api/grafana are PROXIED (orange-cloud) so they sit behind Cloudflare's
# WAF/DDoS/Access (Tenancy P5). lk stays proxied=false: Cloudflare's proxy can't
# pass WebRTC/SIP UDP, and lk uses Caddy's Let's Encrypt cert directly. The
# proxied origins use Authenticated Origin Pulls + a self-signed cert (see
# infra/Caddyfile) so only Cloudflare can reach them.

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

locals {
  manage_dns = var.cloudflare_api_token != "" && var.cloudflare_zone_id != ""
}

resource "cloudflare_dns_record" "api" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "api"
  type    = "A"
  content = google_compute_address.usan.address
  proxied = true
  ttl     = 300
}

resource "cloudflare_dns_record" "lk" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "lk"
  type    = "A"
  content = google_compute_address.usan.address
  proxied = false
  ttl     = 300
}

resource "cloudflare_dns_record" "grafana" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "grafana" # -> grafana.<zone domain>
  type    = "A"
  content = google_compute_address.usan.address
  proxied = true
  ttl     = 300
}

resource "cloudflare_dns_record" "admin" {
  count   = local.manage_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "admin" # -> admin.<zone domain> (Admin UI console, Plan admin-5)
  type    = "A"
  content = google_compute_address.usan.address
  proxied = true
  ttl     = 300
}
