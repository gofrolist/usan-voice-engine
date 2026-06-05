# Plan 4e Workstream B — observability + PHI-access audit-log durability.
#
# Logs reach Cloud Logging via the Ops Agent (startup.sh) reading journald. This file
# adds (B4) a locked, long-retention bucket + sink for the PHI-access audit trail and
# (B5) Cloud Monitoring alerting. The VM SA already has logWriter + metricWriter
# (main.tf). Logging/Monitoring are covered by the existing Google Cloud BAA (confirm
# scope per Plan 4e A4).

resource "google_project_service" "logging" {
  project            = var.project_id
  service            = "logging.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "monitoring" {
  project            = var.project_id
  service            = "monitoring.googleapis.com"
  disable_on_destroy = false
}

# --- B4: immutable PHI-access audit trail (HIPAA §164.312(b)) ---

# Dedicated log bucket with long, LOCKED retention. `locked = true` is a one-way door:
# retention can't be shortened and the bucket can't be deleted before entries expire
# (so `terraform destroy` cannot remove it) — that immutability is the point.
resource "google_logging_project_bucket_config" "audit" {
  project        = var.project_id
  location       = var.region
  bucket_id      = "usan-phi-audit"
  description    = "Immutable PHI-access audit trail (Transcript/Recording access). Plan 4e B4."
  retention_days = var.audit_log_retention_days
  locked         = true
  depends_on     = [google_project_service.logging]
}

# Route the content-free PHI-access audit events into the locked bucket. The filter
# matches whether or not parse_json populated jsonPayload (defensive: covers both the
# structured and raw-text ingestion paths). Same-project bucket sinks need no extra IAM.
resource "google_logging_project_sink" "audit" {
  project     = var.project_id
  name        = "usan-phi-audit-sink"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.audit.id}"

  filter = <<-EOT
    jsonPayload.message="Transcript accessed" OR jsonPayload.message="Recording URL accessed" OR textPayload:"Transcript accessed" OR textPayload:"Recording URL accessed"
  EOT
}

# --- B5: Cloud Monitoring alerting (gated on operator_alert_email) ---

locals {
  alerting    = var.operator_alert_email != ""
  alert_count = local.alerting ? 1 : 0
  api_host    = "api.usanretirement.com"
}

resource "google_monitoring_notification_channel" "operator_email" {
  count        = local.alert_count
  project      = var.project_id
  display_name = "USAN operator email"
  type         = "email"
  labels       = { email_address = var.operator_alert_email }
  depends_on   = [google_project_service.monitoring]
}

# External "is the API up" probe against the public health endpoint.
resource "google_monitoring_uptime_check_config" "api_health" {
  count        = local.alert_count
  project      = var.project_id
  display_name = "usan-api-health"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.api_host
    }
  }
  depends_on = [google_project_service.monitoring]
}

# Service-down: the uptime check is failing (canonical count-of-failures pattern).
resource "google_monitoring_alert_policy" "api_down" {
  count        = local.alert_count
  project      = var.project_id
  display_name = "USAN API health check failing"
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failing"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.\"check_id\"=\"${google_monitoring_uptime_check_config.api_health[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "60s"
      trigger { count = 1 }
      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.\"host\""]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email[0].id]
}

# Resource exhaustion. CPU uses the built-in GCE metric (no agent dependency, 0-1
# scale); memory/disk use Ops Agent metrics (0-100, state=used). Verify these fire as
# expected once the agent is reporting (metric filters aren't checked by terraform).
resource "google_monitoring_alert_policy" "vm_cpu" {
  count        = local.alert_count
  project      = var.project_id
  display_name = "USAN VM CPU > 85%"
  combiner     = "OR"

  conditions {
    display_name = "CPU utilization high"
    condition_threshold {
      filter          = "metric.type=\"compute.googleapis.com/instance/cpu/utilization\" AND resource.type=\"gce_instance\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "300s"
      trigger { count = 1 }
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email[0].id]
}

resource "google_monitoring_alert_policy" "vm_memory" {
  count        = local.alert_count
  project      = var.project_id
  display_name = "USAN VM memory > 85%"
  combiner     = "OR"

  conditions {
    display_name = "Memory used high"
    condition_threshold {
      filter          = "metric.type=\"agent.googleapis.com/memory/percent_used\" AND resource.type=\"gce_instance\" AND metric.label.\"state\"=\"used\""
      comparison      = "COMPARISON_GT"
      threshold_value = 85
      duration        = "300s"
      trigger { count = 1 }
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email[0].id]
}

resource "google_monitoring_alert_policy" "vm_disk" {
  count        = local.alert_count
  project      = var.project_id
  display_name = "USAN VM disk > 85%"
  combiner     = "OR"

  conditions {
    display_name = "Disk used high"
    condition_threshold {
      filter          = "metric.type=\"agent.googleapis.com/disk/percent_used\" AND resource.type=\"gce_instance\" AND metric.label.\"state\"=\"used\""
      comparison      = "COMPARISON_GT"
      threshold_value = 85
      duration        = "300s"
      trigger { count = 1 }
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MEAN"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.label.\"instance_id\""]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator_email[0].id]
}
