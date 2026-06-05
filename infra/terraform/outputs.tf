output "vm_external_ip" {
  description = "Static public IP. Create a DNS A record for your API domain pointing here, and point Telnyx inbound SIP at this IP:5060."
  value       = google_compute_address.usan.address
}

output "ssh_command" {
  description = "SSH into the VM."
  value       = "ssh ${var.ssh_user}@${google_compute_address.usan.address}"
}

output "secret_name" {
  description = "Secret Manager secret to populate with the production .env contents (see Task 6)."
  value       = google_secret_manager_secret.env.secret_id
}

output "recordings_bucket" {
  description = "GCS bucket holding call recordings. Set GCS_BUCKET in the prod .env to this."
  value       = google_storage_bucket.recordings.name
}

output "db_private_ip" {
  description = "Cloud SQL private IP. Prod DATABASE_URL = postgresql://usan:<db_password>@<db_private_ip>:5432/usan?sslmode=require"
  value       = google_sql_database_instance.usan.private_ip_address
}

output "db_connection_name" {
  description = "Cloud SQL connection name (project:region:instance), for the optional Auth Proxy / IAM-auth path."
  value       = google_sql_database_instance.usan.connection_name
}

output "db_password" {
  description = "Generated password for the usan DB user. Read with: terraform output -raw db_password"
  value       = random_password.db.result
  sensitive   = true
}

# --- Plan 4e E: registry + keyless CI (consumed by the CI/compose cutover, PR-2) ---

output "gar_repository" {
  description = "Artifact Registry Docker repo path. Compose image refs become <this>/usan-{api,agent}."
  value       = "${google_artifact_registry_repository.usan.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.usan.repository_id}"
}

output "wif_provider" {
  description = "Full WIF provider resource name for google-github-actions/auth `workload_identity_provider`."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "github_deployer_sa" {
  description = "Deploy SA email for google-github-actions/auth `service_account` (CI impersonates this to push images)."
  value       = google_service_account.github_deployer.email
}
