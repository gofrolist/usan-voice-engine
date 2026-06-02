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
    # Default compute SA + cloud-platform scope; Secret Manager access is
    # narrowed to the one secret via IAM in Task 3.
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

# Grant the VM's default compute service account read access to the secret.
data "google_compute_default_service_account" "default" {}

resource "google_secret_manager_secret_iam_member" "vm_access" {
  secret_id = google_secret_manager_secret.env.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_compute_default_service_account.default.email}"
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

# Telephony + media UDP: SIP signaling, livekit-sip RTP, LiveKit SFU media.
# These are DIRECT to the VM IP — never proxied by Caddy.
resource "google_compute_firewall" "media" {
  name      = "usan-allow-media"
  network   = "default"
  direction = "INGRESS"
  allow {
    protocol = "udp"
    ports = [
      "5060",        # SIP signaling
      "10000-20000", # livekit-sip RTP (widened from dev's 10000-10100)
      "50000-60000", # LiveKit SFU rtc media (widened from dev's 50000-50100)
    ]
  }
  source_ranges = ["0.0.0.0/0"] # Telnyx media origin IPs vary; lock down later if Telnyx publishes ranges.
  target_tags   = ["usan"]
}
