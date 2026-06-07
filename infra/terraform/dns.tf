# === Cloudflare DNS (optional) ===
# When cloudflare_api_token + cloudflare_zone_id are set, Terraform creates the
# api.<domain> and lk.<domain> A records pointing at the VM's static IP.
# proxied = false: Cloudflare's proxy cannot pass WebRTC/SIP UDP, and Caddy's
# Let's Encrypt HTTP-01/TLS-ALPN challenge needs the origin reachable directly.

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
  proxied = false
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
  proxied = false
  ttl     = 300
}
