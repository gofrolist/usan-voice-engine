variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "region" {
  type        = string
  description = "GCP region for the static IP and VM."
  default     = "us-east1"
}

variable "zone" {
  type        = string
  description = "GCP zone for the VM."
  default     = "us-east1-b"
}

variable "machine_type" {
  type        = string
  description = "Compute Engine machine type. e2-standard-4 (4 vCPU / 16GB): bumped from e2-standard-2 because the agent (silero VAD + turn-detector) and egress saturate 2 vCPU under concurrent/burst calls — verified 2026-06-05, a 5-call burst caused turn-detector 5s timeouts, choppy audio, and ~22s answer latency bleeding into other callers. All AI models are external so no GPU is needed."
  default     = "e2-standard-4"
}

variable "boot_disk_gb" {
  type        = number
  description = "Boot disk size in GB (recordings live in GCS, not on disk)."
  default     = 30
}

variable "ssh_user" {
  type        = string
  description = "Login user created on the VM and used by the deploy workflow."
  default     = "usan"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key (contents, not path) authorized for ssh_user."
}

variable "operator_ssh_cidr" {
  type        = string
  description = "CIDR allowed to reach SSH (port 22). Restrict to your IP, e.g. 203.0.113.4/32. Do NOT use 0.0.0.0/0."
}

variable "telnyx_sip_signaling_source_ranges" {
  type = list(string)
  # REQUIRED (no default): who may reach SIP SIGNALING (udp/5060) — i.e. where inbound
  # INVITEs originate. CRITICAL: these are Telnyx's *signaling* IPs, NOT the media/RTP
  # CIDRs. Verified 2026-06-05 against live call traces: inbound INVITEs arrived from
  # 192.76.120.10 and 64.16.250.10 (US signaling, see https://sip.telnyx.com → "SIP
  # signaling addresses"). An earlier mistake put the ~14 media/RTP CIDRs here, so the
  # firewall silently DROPPED every INVITE (caller heard "your call cannot be
  # completed"; Telnyx Debugging showed Status=Init, no response). Use the enclosing
  # /24s for headroom: ["192.76.120.0/24", "64.16.250.0/24"] (also covers the Canada
  # signaling IPs). The media/RTP CIDRs (36.255.198.128/25, 50.114.144.0/21,
  # 64.16.226-230.0/24, 64.16.248/249.0/24, 103.115.244.128/25, 185.246.41/42.x, …)
  # belong on the RTP ports (usan-allow-media), not here. Telnyx rotates/expands these
  # (also published as a machine-readable JSON feed). ["0.0.0.0/0"] = deliberately open.
  description = "Source CIDRs allowed to reach SIP SIGNALING (udp/5060) — Telnyx's signaling IPs (US: 192.76.120.0/24, 64.16.250.0/24), NOT the media/RTP CIDRs. Wrong/stale values silently break inbound calls."
}

variable "secret_name" {
  type        = string
  description = "GCP Secret Manager secret holding the production .env file contents."
  default     = "usan-prod-env"
}

variable "recordings_bucket" {
  type        = string
  description = "Globally-unique GCS bucket name for call recordings."
}

variable "recording_nearline_days" {
  type        = number
  description = "Age in days after which a recording transitions to Nearline storage."
  default     = 30
}

variable "recording_retention_days" {
  type        = number
  description = "Age in days after which a recording is permanently deleted."
  default     = 365
}

variable "recording_noncurrent_retention_days" {
  type        = number
  description = "Days a noncurrent (superseded/deleted) object version is retained before permanent deletion, bounding versioning storage growth."
  default     = 30
}

variable "db_tier" {
  type        = string
  description = "Cloud SQL machine tier (Enterprise edition). db-custom-1-3840 (1 vCPU / 3.75GB) is the cheap baseline for 5k-50k calls/mo; bump to db-custom-2-7680 if needed."
  default     = "db-custom-1-3840"
}

variable "db_availability_type" {
  type        = string
  description = "REGIONAL = synchronous HA standby in a 2nd zone (recommended for prod PHI); ZONAL = single zone, ~half the cost, no automatic failover."
  default     = "REGIONAL"
}

variable "db_disk_gb" {
  type        = number
  description = "Cloud SQL data disk size in GB (autoresizes upward from here)."
  default     = 20
}

variable "cloudflare_api_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Cloudflare API token (Edit zone DNS, scoped to the zone). Empty = manage DNS by hand."
}

variable "cloudflare_zone_id" {
  type        = string
  default     = ""
  description = "Cloudflare zone ID for the domain. Empty (or empty token) = manage DNS by hand."
}

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

variable "audit_log_retention_days" {
  type        = number
  default     = 2190
  description = "Retention (days) for the locked PHI-access audit log bucket. 2190 = 6 years, aligning with HIPAA §164.316(b)(2) documentation-retention guidance. Cloud Logging max is 3650."
}

variable "operator_alert_email" {
  type        = string
  default     = ""
  description = "Email for Cloud Monitoring alert notifications (API-down, CPU/mem/disk). Empty = skip the notification channel + alert policies (no-op)."
}

variable "github_repository" {
  type        = string
  default     = "gofrolist/usan-voice-engine"
  description = "owner/repo permitted to push images via Workload Identity Federation (Plan 4e E). Scopes both the WIF provider attribute_condition and the deploy-SA impersonation principalSet."
}
