# === Managed Postgres (Plan 4d) — Cloud SQL for PostgreSQL ===
# Replaces the in-VM pgvector/pgvector:pg18 container for PRODUCTION. Dev/local
# keeps the container (infra/docker-compose.yml); prod points DATABASE_URL here.

# --- Required APIs ---
resource "google_project_service" "sqladmin" {
  project            = var.project_id
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "servicenetworking" {
  project            = var.project_id
  service            = "servicenetworking.googleapis.com"
  disable_on_destroy = false
}

# --- Private Services Access: reserve a range in the default VPC and peer it to
#     Google's service-producer network so Cloud SQL gets a private, in-VPC IP. ---
data "google_compute_network" "default" {
  name = "default"
}

resource "google_compute_global_address" "sql_private_range" {
  name          = "usan-sql-private-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = data.google_compute_network.default.id
}

resource "google_service_networking_connection" "sql_private_vpc" {
  network                 = data.google_compute_network.default.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.sql_private_range.name]
  depends_on              = [google_project_service.servicenetworking]
}

# --- Strong DB password (kept in Terraform state, never in git). ---
resource "random_password" "db" {
  length  = 32
  special = false # avoids URL-encoding issues inside DATABASE_URL
}

# --- The instance: private IP only, regional HA, daily backups + PITR. ---
resource "google_sql_database_instance" "usan" {
  name                = "usan-pg"
  database_version    = "POSTGRES_18"
  region              = var.region
  deletion_protection = true # PHI safety; see teardown note in the plan
  depends_on          = [google_service_networking_connection.sql_private_vpc]

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = var.db_availability_type
    disk_type         = "PD_SSD"
    disk_size         = var.db_disk_gb
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false # no public IP
      private_network = data.google_compute_network.default.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "08:00" # UTC, off-peak for US contacts
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 14
      }
    }

    maintenance_window {
      day  = 7 # Sunday
      hour = 9 # 09:00 UTC
    }
  }
}

resource "google_sql_database" "usan" {
  name     = "usan"
  instance = google_sql_database_instance.usan.name
}

# The first user is granted cloudsqlsuperuser, which can CREATE EXTENSION for
# allowlisted extensions (pgcrypto now; vector later) — required by migration 0001.
resource "google_sql_user" "usan" {
  name     = "usan"
  instance = google_sql_database_instance.usan.name
  password = random_password.db.result
}

# --- Read-only role login for Grafana (GRANTs live in Alembic migration 0009). ---
resource "random_password" "grafana_ro" {
  length  = 32
  special = false # avoids escaping issues in the .env datasource password
}

resource "google_sql_user" "grafana_ro" {
  name     = "grafana_ro"
  instance = google_sql_database_instance.usan.name
  password = random_password.grafana_ro.result
}
