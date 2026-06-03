# --- Call-recording bucket: LiveKit Egress writes here; the API signs read URLs. ---
resource "google_storage_bucket" "recordings" {
  name                        = var.recordings_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # Retain prior generations of any overwritten/deleted recording (accidental or
  # malicious clobber recovery on PHI); noncurrent versions are reaped below.
  versioning {
    enabled = true
  }

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

  # Bound noncurrent-version accumulation so versioning doesn't grow storage forever.
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = var.recording_noncurrent_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

# Least-privilege split of the former objectAdmin grant (spec §9):
# the API (same SA) only needs to GET objects to sign V4 read URLs...
resource "google_storage_bucket_iam_member" "vm_recordings_read" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.vm.email}"
}

# ...and Egress (on the VM, via ADC) only needs to CREATE recording objects.
# objectCreator grants create without delete/overwrite, so it cannot clobber
# existing recordings.
resource "google_storage_bucket_iam_member" "vm_recordings_write" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectCreator"
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
