# --- Call-recording bucket: LiveKit Egress writes here; the API signs read URLs. ---
resource "google_storage_bucket" "recordings" {
  name                        = var.recordings_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  # Cheaper cold storage after a month, then delete past the retention window (spec §9).
  lifecycle_rule {
    condition {
      age = var.recording_nearline_days
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age = var.recording_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

# Egress (on the VM, via ADC) creates objects; the API (same SA) reads them to sign.
# objectAdmin covers create + get for both roles in one binding.
resource "google_storage_bucket_iam_member" "vm_recordings" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.vm.email}"
}

# Keyless V4 signing: the VM SA signs as ITSELF via IAM signBlob. tokenCreator on the
# SA itself grants iam.serviceAccounts.signBlob.
resource "google_service_account_iam_member" "vm_sign_blob" {
  service_account_id = google_service_account.vm.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.vm.email}"
}

# signBlob requires the IAM Service Account Credentials API.
resource "google_project_service" "iam_credentials" {
  project            = var.project_id
  service            = "iamcredentials.googleapis.com"
  disable_on_destroy = false
}
