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

echo "[startup] done. Compose files are delivered by the deploy workflow (scp), which then runs compose up."
