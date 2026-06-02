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

variable "secret_name" {
  type        = string
  description = "GCP Secret Manager secret holding the production .env file contents."
  default     = "usan-prod-env"
}

variable "image_tag" {
  type        = string
  description = "Container image tag the VM should pull on first boot (passed into the startup script)."
  default     = "latest"
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
