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
  description = "Compute Engine machine type. e2-standard-2 (2 vCPU / 8GB) is the v1 baseline; all AI models are external so no GPU is needed."
  default     = "e2-standard-2"
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
  # REQUIRED (no default): the operator must explicitly choose who may reach SIP
  # signaling (udp/5060) rather than silently leaving it open to the world.
  # Set to Telnyx's CURRENT published SIP signaling CIDRs — verify at
  # https://sip.telnyx.com, which Telnyx rotates/expands and also publishes as a
  # machine-readable JSON feed. As of 2026-06 the published signaling ranges were:
  # 36.255.198.128/25, 50.114.136.128/25, 50.114.144.0/21, 64.16.226.0/24,
  # 64.16.227.0/24, 64.16.228.0/24, 64.16.229.0/24, 64.16.230.0/24, 64.16.248.0/24,
  # 64.16.249.0/24, 103.115.244.128/25, 103.115.247.128/27, 185.246.41.128/25,
  # 185.246.42.128/28. Pass ["0.0.0.0/0"] only to deliberately accept a world-open port.
  description = "Source CIDRs allowed to reach SIP signaling (udp/5060). REQUIRED; set to Telnyx's current published signaling CIDRs (see https://sip.telnyx.com). Wrong/stale values silently break inbound calls."
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
