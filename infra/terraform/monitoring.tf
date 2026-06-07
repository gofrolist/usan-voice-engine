# === Self-hosted monitoring stack (Grafana + Prometheus) — MON-2 ===
# The grafana DNS record lives in dns.tf and the grafana_ro DB user in database.tf
# (next to their siblings). This file holds the Grafana-stack-specific resources.

# Grafana admin login password (folded into the prod .env blob, like db_password —
# surfaced via the grafana_admin_password output).
resource "random_password" "grafana_admin" {
  length  = 24
  special = false
}

# roles/monitoring.viewer lets a Grafana Cloud Monitoring datasource (MON-3 System
# dashboard) read host CPU/mem/disk. Read-only; granted now as prep for MON-3. The
# VM SA otherwise has only metricWriter.
resource "google_project_iam_member" "vm_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.vm.email}"
}
