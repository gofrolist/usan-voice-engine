#!/usr/bin/env bash
set -euo pipefail

SECRET_NAME="${secret_name}"
APP_DIR="/opt/usan"

echo "[startup] installing docker..."
apt-get update -y
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
# --yes so re-runs (VM reboot/reset) overwrite the existing keyring instead of
# prompting on a /dev/tty that doesn't exist in the metadata script runner.
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

usermod -aG docker "${ssh_user}" || true

echo "[startup] materializing app dir + .env from Secret Manager..."
mkdir -p "$APP_DIR/infra"
# The VM's service account has secretmanager.secretAccessor (Task 3).
gcloud secrets versions access latest --secret="$SECRET_NAME" > "$APP_DIR/infra/.env"
chmod 600 "$APP_DIR/infra/.env"
chown -R "${ssh_user}:${ssh_user}" "$APP_DIR"

echo "[startup] installing Google Cloud Ops Agent (logs + metrics -> Cloud Logging/Monitoring)..."
# Wrapped in a function called with `|| echo WARN` so a transient download/install
# failure is NON-FATAL: observability must never abort the VM boot after the critical
# docker + .env steps already ran. Idempotent: apt re-install is a no-op on reboot and
# the config is rewritten each boot.
install_ops_agent() {
  curl -fsSL -o /tmp/add-ops-agent-repo.sh https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
  bash /tmp/add-ops-agent-repo.sh --also-install
  # Ingest journald (system logs + container stdout under the journald log driver) into
  # Cloud Logging, parsing our app's JSON so the PHI-access audit fields (call_id,
  # client, segments) are queryable. No metrics section => built-in hostmetrics stay on.
  install -d /etc/google-cloud-ops-agent
  cat > /etc/google-cloud-ops-agent/config.yaml <<'OPSCFG'
logging:
  receivers:
    journald:
      type: systemd_journald
  processors:
    parse_app_json:
      type: parse_json
  service:
    pipelines:
      journald:
        receivers: [journald]
        processors: [parse_app_json]
OPSCFG
  systemctl restart google-cloud-ops-agent
}
install_ops_agent || echo "[startup] WARN: Ops Agent setup failed (non-fatal); app is unaffected."

echo "[startup] done. Compose files are delivered by the deploy workflow (scp), which then runs compose up."
