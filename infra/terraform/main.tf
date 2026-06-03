# --- Static external IP (Telnyx points inbound SIP here; survives VM recreation) ---
resource "google_compute_address" "usan" {
  name   = "usan-ip"
  region = var.region
}

# --- The single application VM ---
resource "google_compute_instance" "usan" {
  name         = "usan-vm"
  machine_type = var.machine_type
  zone         = var.zone

  tags = ["usan"] # firewall target tag (Task 3)

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.usan.address
    }
  }

  metadata = {
    ssh-keys = "${var.ssh_user}:${var.ssh_public_key}"
    startup-script = templatefile("${path.module}/startup.sh", {
      secret_name = var.secret_name
      ssh_user    = var.ssh_user
    })
  }

  service_account {
    # Dedicated least-privilege SA (defined below). cloud-platform scope is the
    # modern default — IAM roles, not legacy scopes, gate actual access.
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  allow_stopping_for_update = true
}

# --- Secret Manager: container for the production .env (content added out-of-band) ---
resource "google_secret_manager_secret" "env" {
  secret_id = var.secret_name
  replication {
    auto {}
  }
}

# Dedicated least-privilege runtime SA for the VM, instead of the default
# compute SA (which typically carries broad project Editor).
resource "google_service_account" "vm" {
  account_id   = "usan-vm"
  display_name = "USAN VM runtime"
}

# Read access to the one prod-env secret only.
resource "google_secret_manager_secret_iam_member" "vm_access" {
  secret_id = google_secret_manager_secret.env.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

# Minimal ops roles so the VM can ship logs/metrics (scoped replacements for
# what the default SA's Editor would have covered).
resource "google_project_iam_member" "vm_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "vm_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- Firewall ---
# SSH — operator CIDR only.
resource "google_compute_firewall" "ssh" {
  name      = "usan-allow-ssh"
  network   = "default"
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = [var.operator_ssh_cidr]
  target_tags   = ["usan"]
}

# HTTPS (Caddy) + HTTP (ACME challenge / redirect).
resource "google_compute_firewall" "web" {
  name      = "usan-allow-web"
  network   = "default"
  direction = "INGRESS"
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
  allow {
    protocol = "udp"
    ports    = ["443"] # HTTP/3
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["usan"]
}

# Media/RTP UDP: livekit-sip RTP, LiveKit SFU media. DIRECT to the VM IP —
# never proxied by Caddy. These stay at 0.0.0.0/0: the originating media
# (RTP) source IPs vary per call/relay and cannot be pinned to a stable CIDR
# without dropping legitimate audio. Signaling (5060) is split out below so it
# CAN be locked down without affecting media.
resource "google_compute_firewall" "media" {
  name      = "usan-allow-media"
  network   = "default"
  direction = "INGRESS"
  allow {
    protocol = "udp"
    ports = [
      "10000-20000", # livekit-sip RTP (widened from dev's 10000-10100)
      "50000-60000", # LiveKit SFU rtc media (widened from dev's 50000-50100)
    ]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["usan"]
}

# SIP signaling (udp/5060) split out so it can be restricted to Telnyx's
# published signaling CIDRs. Defaults to 0.0.0.0/0 to preserve current
# behavior; set var.telnyx_sip_signaling_source_ranges to lock it down.
resource "google_compute_firewall" "sip" {
  name      = "usan-allow-sip"
  network   = "default"
  direction = "INGRESS"
  allow {
    protocol = "udp"
    ports    = ["5060"]
  }
  source_ranges = var.telnyx_sip_signaling_source_ranges
  target_tags   = ["usan"]
}
