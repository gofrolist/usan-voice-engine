# Plan 4a — Deploy & TLS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand the existing Docker Compose stack up on a public GCP VM behind TLS, with a tag-driven GHCR build/deploy pipeline, then run the live telephony smoke tests that every prior plan deferred for lack of a public IP.

**Architecture:** Terraform provisions a single GCP Compute Engine VM with a static external IP, a firewall opened for SIP/RTP/LiveKit-media UDP + HTTPS, and a startup script that installs Docker and loads secrets from GCP Secret Manager into `/opt/usan/infra/.env`. Caddy terminates TLS (443) and reverse-proxies the FastAPI service (and, optionally, LiveKit's WS signaling); **WebRTC media and SIP stay as direct UDP to the VM's public IP — they are never proxied through Caddy.** GitHub Actions builds multi-arch images to GHCR on `main`/tags and, on a version tag, SSHes to the VM to `docker compose pull && up -d` and health-checks `/health`. The API container already runs `alembic upgrade head` on start, so deploy needs no separate migration step.

**Tech Stack:** Terraform (`google` provider), GCP Compute Engine + Secret Manager, Docker Compose overlays, Caddy 2 (automatic HTTPS via Let's Encrypt), GitHub Actions (`docker/build-push-action`, `appleboy/ssh-action`), GHCR, Python stdlib (smoke-test script).

> **Scope note.** This plan is **deploy infrastructure only** — no application code changes. The recording subsystem (LiveKit Egress → GCS, `/webhooks/livekit/egress`, presigned URLs on `GET /v1/calls/{id}`) is the immediate follow-on, **Plan 4b — Egress & Recording**, and depends on the GCS bucket + service-account this plan creates. RAG, DTMF, reconnection hardening, and observability are later plans.

> **Decision (2026-06-01).** Host + object storage = **GCP**: Compute Engine VM + Google Cloud Storage. This resolves the two open infra questions in the design spec §16 ("Hetzner vs AWS VM", "B2 vs S3").

---

## Prerequisites (operator, before Task 1)

These are one-time manual setups the engineer needs in hand. They are **not** code tasks but the plan cannot be validated end-to-end without them:

1. A GCP project with billing enabled; `gcloud` authenticated locally (`gcloud auth application-default login`) and `gcloud config set project <PROJECT_ID>`.
2. APIs enabled: `gcloud services enable compute.googleapis.com secretmanager.googleapis.com`.
3. A registered domain you control, e.g. `usan.example`, with the ability to create an A record (Caddy needs a real DNS name pointing at the VM IP to issue certs).
4. A GitHub repo with GHCR enabled (default for the org) and the ability to add Actions secrets.
5. Telnyx number + SIP trunk already configured (per `infra/README.md`); inbound SIP signaling URI will be pointed at the VM's public IP on UDP 5060 in Task 10.

---

## File Structure

New and modified files, grouped by responsibility:

**Terraform (`infra/terraform/`) — VM, network, secrets:**
- Create `infra/terraform/versions.tf` — provider + required version pins.
- Create `infra/terraform/variables.tf` — inputs (project, region, zone, machine type, SSH key, operator CIDR, domain).
- Create `infra/terraform/main.tf` — static IP, Compute Engine VM, startup script, Secret Manager secret container + IAM, firewall rules.
- Create `infra/terraform/outputs.tf` — VM external IP, SSH command, secret name.
- Create `infra/terraform/startup.sh` — VM boot script (install Docker, fetch `.env`).
- Create `infra/terraform/terraform.tfvars.example` — copy-to-`terraform.tfvars` template.

**Compose overlays + TLS (`infra/`):**
- Create `infra/Caddyfile` — TLS reverse proxy for API (+ optional LiveKit signaling).
- Create `infra/docker-compose.tls.yml` — Caddy service overlay.
- Create `infra/docker-compose.prod.yml` — GHCR images, public UDP binds, widened ranges, `use_external_ip: true`.
- Create `infra/.env.prod.example` — production env template (wss URLs, domains, image tag, GHCR owner).
- Modify `infra/README.md` — production deploy runbook + smoke-test results section.

**CI/CD (`.github/workflows/`):**
- Create `.github/workflows/build.yml` — multi-arch GHCR build/push (agent-base → agent → api) on `main` + tags.
- Create `.github/workflows/deploy.yml` — on tag, SSH to VM, `pull && up -d`, health check.

**Smoke-test tooling (`scripts/`):**
- Create `scripts/place_test_call.py` — stdlib outbound-call placer (matches spec §13).
- Create `scripts/tests/test_place_test_call.py` — unit test for its request builder.
- Modify `.github/workflows/test.yml` — add a job that runs the script's unit test.

---

## Task 1: Terraform provider scaffolding

**Files:**
- Create: `infra/terraform/versions.tf`
- Create: `infra/terraform/variables.tf`
- Create: `infra/terraform/terraform.tfvars.example`
- Modify: `.gitignore`

- [ ] **Step 1: Write `infra/terraform/versions.tf`**

```hcl
terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}
```

- [ ] **Step 2: Write `infra/terraform/variables.tf`**

```hcl
variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "region" {
  type        = string
  description = "GCP region for the static IP and VM."
  default     = "us-east1"
}

variable "zone" {
  type        = string
  description = "GCP zone for the VM."
  default     = "us-east1-b"
}

variable "machine_type" {
  type        = string
  description = "Compute Engine machine type. e2-standard-2 (2 vCPU / 8GB) is the v1 baseline; all AI models are external so no GPU is needed."
  default     = "e2-standard-2"
}

variable "boot_disk_gb" {
  type        = number
  description = "Boot disk size in GB (recordings live in GCS, not on disk)."
  default     = 30
}

variable "ssh_user" {
  type        = string
  description = "Login user created on the VM and used by the deploy workflow."
  default     = "usan"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key (contents, not path) authorized for ssh_user."
}

variable "operator_ssh_cidr" {
  type        = string
  description = "CIDR allowed to reach SSH (port 22). Restrict to your IP, e.g. 203.0.113.4/32. Do NOT use 0.0.0.0/0."
}

variable "secret_name" {
  type        = string
  description = "GCP Secret Manager secret holding the production .env file contents."
  default     = "usan-prod-env"
}

variable "image_tag" {
  type        = string
  description = "Container image tag the VM should pull on first boot (passed into the startup script)."
  default     = "latest"
}
```

- [ ] **Step 3: Write `infra/terraform/terraform.tfvars.example`**

```hcl
# Copy to terraform.tfvars and fill in. terraform.tfvars is gitignored (Step 4).
project_id        = "your-gcp-project-id"
region            = "us-east1"
zone              = "us-east1-b"
ssh_user          = "usan"
ssh_public_key    = "ssh-ed25519 AAAA... you@host"
operator_ssh_cidr = "203.0.113.4/32" # your workstation IP /32
# image_tag       = "latest"
```

- [ ] **Step 4: Gitignore Terraform local state and secrets**

Append to the repo root `.gitignore` (create the block if absent):

```gitignore
# Terraform
infra/terraform/.terraform/
infra/terraform/*.tfstate
infra/terraform/*.tfstate.*
infra/terraform/terraform.tfvars
infra/terraform/.terraform.lock.hcl
```

- [ ] **Step 5: Validate formatting and syntax**

Run: `cd infra/terraform && terraform fmt -check && terraform init -backend=false && terraform validate`
Expected: `terraform fmt -check` prints nothing (exit 0); `terraform validate` prints `Success! The configuration is valid.` (Tasks 2–3 add the resources `validate` will then also check — at this step only providers/vars exist, which is valid on its own.)

- [ ] **Step 6: Commit**

```bash
git add infra/terraform/versions.tf infra/terraform/variables.tf infra/terraform/terraform.tfvars.example .gitignore
git commit -m "infra: terraform provider scaffolding for GCP deploy"
```

---

## Task 2: Terraform — VM, static IP, and boot startup script

**Files:**
- Create: `infra/terraform/startup.sh`
- Create: `infra/terraform/main.tf` (VM + IP portion; firewall + secrets added in Task 3)

- [ ] **Step 1: Write `infra/terraform/startup.sh`**

This runs as root on first boot. It installs Docker and materializes the `.env` from Secret Manager. It does **not** run `compose up` — the deploy workflow (Task 8) `scp`s the compose files and brings the stack up, so the compose definitions always match the deployed tag.

```bash
#!/usr/bin/env bash
set -euo pipefail

SECRET_NAME="${secret_name}"
APP_DIR="/opt/usan"

echo "[startup] installing docker..."
apt-get update -y
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
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
```

> Note: `${secret_name}` and `${ssh_user}` are Terraform `templatefile()` interpolations, filled in Step 2. `gcloud` is preinstalled on GCP's Debian images.

- [ ] **Step 2: Write the VM + static IP into `infra/terraform/main.tf`**

```hcl
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
```

- [ ] **Step 3: Validate**

Run: `cd infra/terraform && terraform fmt -check && terraform validate`
Expected: `fmt -check` exit 0; `validate` → `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/startup.sh infra/terraform/main.tf
git commit -m "infra: terraform compute engine VM + static IP + boot script"
```

---

## Task 3: Terraform — firewall, Secret Manager, IAM, outputs

**Files:**
- Modify: `infra/terraform/main.tf` (append firewall + secret + IAM)
- Create: `infra/terraform/outputs.tf`

- [ ] **Step 1: Append firewall + secret resources to `infra/terraform/main.tf`**

```hcl
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
```

- [ ] **Step 2: Write `infra/terraform/outputs.tf`**

```hcl
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
```

- [ ] **Step 3: Validate**

Run: `cd infra/terraform && terraform fmt -check && terraform validate`
Expected: `fmt -check` exit 0; `validate` → `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/main.tf infra/terraform/outputs.tf
git commit -m "infra: terraform firewall, secret manager, IAM, and outputs"
```

---

## Task 4: Caddy TLS reverse proxy

**Files:**
- Create: `infra/Caddyfile`
- Create: `infra/docker-compose.tls.yml`

Caddy fronts the **API** over HTTPS (the load-bearing external surface: external systems POST `/v1/calls`, callers fetch `/v1/calls/{id}`). The LiveKit WS block is included for spec §10 completeness (admin/`livekit-cli` over the internet); **WebRTC media + SIP are UDP and bypass Caddy entirely.**

- [ ] **Step 1: Write `infra/Caddyfile`**

```caddyfile
# {$API_DOMAIN} and {$LIVEKIT_DOMAIN} come from the Caddy container env.
{$API_DOMAIN} {
	encode zstd gzip
	reverse_proxy api:8000
}

{$LIVEKIT_DOMAIN} {
	reverse_proxy livekit:7880
}
```

- [ ] **Step 2: Write `infra/docker-compose.tls.yml`**

```yaml
# TLS overlay — adds Caddy in front of api (+ livekit signaling).
# Use together with the base + prod overlays:
#   docker compose --env-file infra/.env \
#     -f infra/docker-compose.yml \
#     -f infra/docker-compose.prod.yml \
#     -f infra/docker-compose.tls.yml up -d
services:
  caddy:
    image: caddy:2-alpine
    container_name: usan-caddy
    init: true
    depends_on:
      - api
      - livekit
    environment:
      API_DOMAIN: ${API_DOMAIN}
      LIVEKIT_DOMAIN: ${LIVEKIT_DOMAIN}
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp" # HTTP/3
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    restart: unless-stopped

volumes:
  caddy_data:
  caddy_config:
```

- [ ] **Step 3: Validate that Caddy renders against the base file**

Run (from repo root, with a throwaway env so substitution succeeds):
```bash
API_DOMAIN=api.usan.example LIVEKIT_DOMAIN=lk.usan.example \
docker compose \
  -f infra/docker-compose.yml \
  -f infra/docker-compose.tls.yml \
  --env-file infra/.env.example config | grep -E "usan-caddy|443"
```
Expected: prints the `usan-caddy` container and the `443` port mappings with no compose error. *(Combined base+prod+tls rendering is validated in Task 5, Step 2.)*

- [ ] **Step 4: Commit**

```bash
git add infra/Caddyfile infra/docker-compose.tls.yml
git commit -m "infra: caddy TLS reverse proxy overlay for api + livekit signaling"
```

---

## Task 5: Production compose overlay (GHCR images, public media, widened ranges)

**Files:**
- Create: `infra/docker-compose.prod.yml`

This overlay (a) swaps locally-built images for GHCR images, (b) widens the dev-sized UDP port ranges, (c) flips LiveKit to advertise the VM's public IP for ICE, and (d) keeps the API on a loopback-only host publish (Caddy reaches it over the compose network).

- [ ] **Step 1: Write `infra/docker-compose.prod.yml`**

```yaml
# Production overlay. Layer on top of the base file:
#   docker compose --env-file infra/.env \
#     -f infra/docker-compose.yml \
#     -f infra/docker-compose.prod.yml \
#     -f infra/docker-compose.tls.yml up -d
#
# Requires in .env: IMAGE_TAG, GHCR_OWNER, API_DOMAIN, LIVEKIT_DOMAIN,
# and LIVEKIT_URL=ws://livekit:7880 (internal hop stays plaintext on the
# compose network; external TLS is terminated by Caddy).
services:
  api:
    image: ghcr.io/${GHCR_OWNER}/usan-api:${IMAGE_TAG:-latest}
    pull_policy: always
    build: !reset null
    # API is reached by Caddy over the compose network as api:8000.
    # Keep a loopback publish so on-VM `curl localhost:8000/health` works.
    ports: !override
      - "127.0.0.1:8000:8000"

  agent:
    image: ghcr.io/${GHCR_OWNER}/usan-agent:${IMAGE_TAG:-latest}
    pull_policy: always
    build: !reset null

  livekit:
    ports: !override
      - "127.0.0.1:7880:7880"
      - "127.0.0.1:7881:7881/tcp"
      - "50000-60000:50000-60000/udp"
    environment:
      LIVEKIT_CONFIG: |
        port: 7880
        redis:
          address: redis:6379
        rtc:
          tcp_port: 7881
          port_range_start: 50000
          port_range_end: 60000
          use_external_ip: true
        keys:
          ${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}
        logging:
          level: info
          json: true
        turn:
          enabled: false
        room:
          empty_timeout: 60
          max_participants: 10
        webhook:
          api_key: ${LIVEKIT_API_KEY}
          urls:
            - http://api:8000/webhooks/livekit

  livekit-sip:
    ports: !override
      - "5060:5060/udp"
      - "10000-20000:10000-20000/udp"
    environment:
      SIP_CONFIG_BODY: |
        api_key: ${LIVEKIT_API_KEY}
        api_secret: ${LIVEKIT_API_SECRET}
        ws_url: ${LIVEKIT_URL}
        redis:
          address: redis:6379
        sip_port: 5060
        rtp_port: 10000-20000
        logging:
          level: info
          json: true
```

> `!reset` and `!override` are Docker Compose merge tags (Compose v2.24+): `build: !reset null` drops the base file's `build:` so only the GHCR `image:` is used; `ports: !override` replaces (rather than appends to) the base port list so dev ranges don't linger.

- [ ] **Step 2: Validate the prod overlay renders and resolves images**

Run:
```bash
GHCR_OWNER=gofrolist IMAGE_TAG=latest API_DOMAIN=api.usan.example LIVEKIT_DOMAIN=lk.usan.example \
docker compose \
  -f infra/docker-compose.yml \
  -f infra/docker-compose.prod.yml \
  -f infra/docker-compose.tls.yml \
  --env-file infra/.env.example config | grep -E "image:|use_external_ip|50000-60000|10000-20000"
```
Expected output includes `ghcr.io/gofrolist/usan-api:latest`, `ghcr.io/gofrolist/usan-agent:latest`, `use_external_ip: true`, and the widened UDP ranges; no `build:` context under `api`/`agent`.

- [ ] **Step 3: Commit**

```bash
git add infra/docker-compose.prod.yml
git commit -m "infra: production compose overlay (GHCR images, public media, widened UDP)"
```

---

## Task 6: Production env template + deploy runbook

**Files:**
- Create: `infra/.env.prod.example`
- Modify: `infra/README.md`

- [ ] **Step 1: Write `infra/.env.prod.example`**

This is the file whose **filled-in** contents get stored in GCP Secret Manager and materialized to `/opt/usan/infra/.env` on the VM at boot.

```bash
# === Production .env — store the FILLED copy in GCP Secret Manager (secret: usan-prod-env) ===
# Do NOT commit the filled version. The VM startup script writes it to /opt/usan/infra/.env.

# --- Images (set by the deploy workflow / operator) ---
GHCR_OWNER=gofrolist
IMAGE_TAG=latest

# --- Domains (must have DNS A records -> VM static IP) ---
API_DOMAIN=api.usan.example
LIVEKIT_DOMAIN=lk.usan.example

# === Postgres ===
POSTGRES_USER=usan
POSTGRES_PASSWORD=__STRONG_RANDOM__
POSTGRES_DB=usan
DATABASE_URL=postgresql://usan:__STRONG_RANDOM__@postgres:5432/usan

# === LiveKit SFU ===
LIVEKIT_API_KEY=__openssl rand -hex 16__
LIVEKIT_API_SECRET=__openssl rand -hex 32__
# Internal compose hop stays plaintext; external TLS is terminated by Caddy.
LIVEKIT_URL=ws://livekit:7880

# === Cartesia / Gemini ===
CARTESIA_API_KEY=
DEFAULT_CARTESIA_VOICE_ID=
GEMINI_API_KEY=

# === Telnyx (SIP trunk) ===
TELNYX_SIP_USERNAME=
TELNYX_SIP_PASSWORD=
TELNYX_SIP_HOST=sip.telnyx.com
TELNYX_INBOUND_DID=
TELNYX_CALLER_ID=
LIVEKIT_SIP_OUTBOUND_TRUNK_ID=

# === Service-to-service auth ===
JWT_SIGNING_KEY=__openssl rand -hex 32__
API_BASE_URL=http://api:8000

# === Misc ===
LOG_LEVEL=INFO
AGENT_NAME=usan-agent
```

- [ ] **Step 2: Add a "Production deploy (Plan 4a)" section to `infra/README.md`**

Insert this section near the top of `infra/README.md`, after the `# infra — manual setup` heading block:

````markdown
## Production deploy (Plan 4a — GCP)

One-time provisioning:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in project, ssh key, your /32
terraform init
terraform apply
terraform output vm_external_ip                # note this IP
```

Then:

1. **DNS:** create A records `api.<domain>` and `lk.<domain>` -> the `vm_external_ip`.
2. **Secrets:** fill a copy of `infra/.env.prod.example` and push it to Secret Manager:
   ```bash
   gcloud secrets versions add usan-prod-env --data-file=/path/to/filled.env
   ```
   (The VM reads `latest` at boot; re-run this command + reboot/redeploy to rotate.)
3. **Telnyx:** point the trunk's inbound SIP signaling URI at `<vm_external_ip>:5060` (UDP).
4. **First deploy:** push a version tag (`git tag v0.1.0 && git push origin v0.1.0`) — `deploy.yml` ships the compose files and brings the stack up. Or deploy manually:
   ```bash
   ssh usan@<vm_external_ip>
   # compose files were scp'd to /opt/usan/infra by the workflow; to do it by hand,
   # copy infra/*.yml + infra/Caddyfile there, then:
   cd /opt/usan
   docker compose --env-file infra/.env \
     -f infra/docker-compose.yml \
     -f infra/docker-compose.prod.yml \
     -f infra/docker-compose.tls.yml up -d
   ```
5. **Verify TLS:** `curl -fsS https://api.<domain>/health` -> `{"status":"ok"}`.
````

- [ ] **Step 3: Validate the example env covers every key the overlays interpolate**

Run:
```bash
# Every ${VAR} referenced by the base + prod overlays must exist in .env.prod.example.
comm -23 \
  <(grep -ohE '\$\{[A-Z_]+' infra/docker-compose.yml infra/docker-compose.prod.yml | tr -d '${' | sort -u) \
  <(grep -oE '^[A-Z_]+' infra/.env.prod.example | sort -u)
```
Expected: prints nothing (every interpolated var has a key in the example). If a var prints, add it to `infra/.env.prod.example`.

- [ ] **Step 4: Commit**

```bash
git add infra/.env.prod.example infra/README.md
git commit -m "infra: production env template and GCP deploy runbook"
```

---

## Task 7: CI — multi-arch GHCR build/push

**Files:**
- Create: `.github/workflows/build.yml`

Builds three images and pushes to GHCR: the agent **base** (heavy model pre-warm), the agent **app** (built `FROM` the pushed base via the `BASE_IMAGE` build-arg), and the **api**. Targets `linux/amd64` (the GCP VM is amd64; arm64 is deferred — the base image's model pre-warm makes arm64 emulation builds prohibitively slow).

- [ ] **Step 1: Write `.github/workflows/build.yml`**

```yaml
name: Build

on:
  push:
    branches: [main]
    tags: ["v*"]

env:
  REGISTRY: ghcr.io

jobs:
  build:
    name: Build & push images
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v6

      - uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Compute image tag
        id: tag
        run: |
          if [ "${GITHUB_REF_TYPE}" = "tag" ]; then
            echo "value=${GITHUB_REF_NAME}" >> "$GITHUB_OUTPUT"
          else
            echo "value=latest" >> "$GITHUB_OUTPUT"
          fi

      - name: Build & push api
        uses: docker/build-push-action@v6
        with:
          context: apps/api
          file: apps/api/Dockerfile
          platforms: linux/amd64
          push: true
          tags: |
            ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-api:${{ steps.tag.outputs.value }}
            ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-api:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Build & push agent base (model pre-warm)
        uses: docker/build-push-action@v6
        with:
          context: services/agent
          file: services/agent/Dockerfile.base
          platforms: linux/amd64
          push: true
          tags: ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-agent-base:${{ steps.tag.outputs.value }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Build & push agent app
        uses: docker/build-push-action@v6
        with:
          context: services/agent
          file: services/agent/Dockerfile
          platforms: linux/amd64
          push: true
          build-args: |
            BASE_IMAGE=${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-agent-base:${{ steps.tag.outputs.value }}
          tags: |
            ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-agent:${{ steps.tag.outputs.value }}
            ${{ env.REGISTRY }}/${{ github.repository_owner }}/usan-agent:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 2: Validate with actionlint**

Run: `actionlint .github/workflows/build.yml`
Expected: no output (exit 0). *(If `actionlint` isn't installed: `brew install actionlint`. The repo already runs actionlint in pre-commit per spec §13.)*

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/build.yml
git commit -m "ci: multi-arch GHCR build/push for api + agent (base + app)"
```

---

## Task 8: CI — deploy on tag

**Files:**
- Create: `.github/workflows/deploy.yml`

On a `v*` tag, after images exist in GHCR, `scp` the compose files + Caddyfile to the VM and `docker compose pull && up -d`, then health-check. The API entrypoint runs `alembic upgrade head` itself, so no separate migration step is needed.

- [ ] **Step 1: Add required GitHub Actions secrets (operator, documented here)**

The deploy job needs these repo secrets:
- `DEPLOY_SSH_KEY` — private key whose public half is in `var.ssh_public_key` (Task 1).
- `DEPLOY_HOST` — the VM static IP (`terraform output vm_external_ip`).
- `DEPLOY_USER` — the `ssh_user` (default `usan`).
- `API_DOMAIN` — e.g. `api.usan.example` (for the post-deploy health check).

- [ ] **Step 2: Write `.github/workflows/deploy.yml`**

```yaml
name: Deploy

on:
  push:
    tags: ["v*"]

jobs:
  deploy:
    name: Deploy to VM
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - name: Copy compose files to VM
        uses: appleboy/scp-action@v0.1.7
        with:
          host: ${{ secrets.DEPLOY_HOST }}
          username: ${{ secrets.DEPLOY_USER }}
          key: ${{ secrets.DEPLOY_SSH_KEY }}
          source: "infra/docker-compose.yml,infra/docker-compose.prod.yml,infra/docker-compose.tls.yml,infra/Caddyfile"
          target: "/opt/usan"
          overwrite: true

      - name: Pull images and bring stack up
        uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.DEPLOY_HOST }}
          username: ${{ secrets.DEPLOY_USER }}
          key: ${{ secrets.DEPLOY_SSH_KEY }}
          envs: GITHUB_REF_NAME
          script: |
            set -euo pipefail
            cd /opt/usan
            export IMAGE_TAG="${GITHUB_REF_NAME}"
            COMPOSE="docker compose --env-file infra/.env \
              -f infra/docker-compose.yml \
              -f infra/docker-compose.prod.yml \
              -f infra/docker-compose.tls.yml"
            $COMPOSE pull
            $COMPOSE up -d
            $COMPOSE ps

      - name: Post-deploy health check
        run: |
          set -e
          for i in $(seq 1 30); do
            if curl -fsS "https://${{ secrets.API_DOMAIN }}/health" | grep -q '"status":"ok"'; then
              echo "healthy"; exit 0
            fi
            echo "waiting for health... ($i)"; sleep 5
          done
          echo "health check failed"; exit 1
```

> `deploy.yml` and `build.yml` are separate workflows both triggered by the tag; the `$COMPOSE pull` step waits on GHCR. If you prefer a hard ordering, merge build+deploy into one workflow with `needs: build` — an optional follow-up, not required for v1.

- [ ] **Step 3: Validate with actionlint**

Run: `actionlint .github/workflows/deploy.yml`
Expected: no output (exit 0).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci: tag-driven deploy to VM with post-deploy health check"
```

---

## Task 9: Smoke-test script `scripts/place_test_call.py`

**Files:**
- Create: `scripts/place_test_call.py`
- Create: `scripts/tests/test_place_test_call.py`
- Modify: `.github/workflows/test.yml`

Stdlib-only (no deps, runnable as `python3 scripts/place_test_call.py`), matching spec §13's invocation. The request-building logic is a pure function so it's unit-testable.

- [ ] **Step 1: Write the failing test `scripts/tests/test_place_test_call.py`**

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from place_test_call import build_request  # noqa: E402


def test_build_request_targets_calls_endpoint():
    req = build_request(
        base_url="https://api.usan.example",
        elder_id="11111111-1111-1111-1111-111111111111",
        idempotency_key="smoke-1",
        dynamic_vars={"greeting": "hi"},
    )
    assert req.full_url == "https://api.usan.example/v1/calls"
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode())
    assert body == {
        "elder_id": "11111111-1111-1111-1111-111111111111",
        "idempotency_key": "smoke-1",
        "dynamic_vars": {"greeting": "hi"},
    }


def test_build_request_strips_trailing_slash_on_base_url():
    req = build_request(
        base_url="https://api.usan.example/",
        elder_id="e",
        idempotency_key="k",
        dynamic_vars={},
    )
    assert req.full_url == "https://api.usan.example/v1/calls"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest scripts/tests/test_place_test_call.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'place_test_call'`.

- [ ] **Step 3: Write `scripts/place_test_call.py`**

```python
#!/usr/bin/env python3
"""Place a single outbound test call against a running USAN API.

Usage:
    python3 scripts/place_test_call.py --elder-id <UUID> [--base-url URL] [--key KEY]

Stdlib only — runnable anywhere Python 3 exists, no project/uv needed.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def build_request(
    *,
    base_url: str,
    elder_id: str,
    idempotency_key: str,
    dynamic_vars: dict,
) -> urllib.request.Request:
    """Build the POST /v1/calls request (pure — no network)."""
    url = base_url.rstrip("/") + "/v1/calls"
    payload = {
        "elder_id": elder_id,
        "idempotency_key": idempotency_key,
        "dynamic_vars": dynamic_vars,
    }
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Place a USAN outbound test call.")
    parser.add_argument("--elder-id", required=True, help="Elder UUID to call.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL.")
    parser.add_argument("--key", default="smoke-1", help="Idempotency key.")
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="K=V",
        help="dynamic_vars entry (repeatable).",
    )
    args = parser.parse_args()

    dynamic_vars: dict[str, str] = {}
    for pair in args.var:
        k, _, v = pair.partition("=")
        dynamic_vars[k] = v

    req = build_request(
        base_url=args.base_url,
        elder_id=args.elder_id,
        idempotency_key=args.key,
        dynamic_vars=dynamic_vars,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (trusted operator URL)
            print(resp.read().decode())
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        return 1
    except urllib.error.URLError as e:
        print(f"request failed: {e.reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest scripts/tests/test_place_test_call.py -v`
Expected: 2 passed.

- [ ] **Step 5: Add a CI job to `.github/workflows/test.yml`**

Append this job (sibling to `pytest-api` / `pytest-agent`):

```yaml
  pytest-scripts:
    name: pytest (scripts)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install pytest
        run: pip install pytest
      - name: pytest
        run: python -m pytest scripts/tests -v --tb=short
```

- [ ] **Step 6: Validate the new job lints**

Run: `actionlint .github/workflows/test.yml`
Expected: no output (exit 0).

- [ ] **Step 7: Commit**

```bash
git add scripts/place_test_call.py scripts/tests/test_place_test_call.py .github/workflows/test.yml
git commit -m "ci: add place_test_call smoke script with unit test"
```

---

## Task 10: Live telephony smoke tests + record results

This is the payoff: the validation deferred across Plans 1, 2a, 2b-1, and 2b-2. Run against the deployed VM. Each sub-step records its observed result in `infra/README.md` under a new "## Live smoke results (Plan 4a)" section so the evidence is durable.

**Files:**
- Modify: `infra/README.md` (append the results section as you go)

- [ ] **Step 1: Confirm the stack is healthy on the VM**

Run (from your workstation): `curl -fsS https://api.<domain>/health`
Expected: `{"status":"ok"}`. If this fails, stop and debug deploy (Caddy cert issuance needs the DNS A record live; `ssh` in and check `docker compose ... logs caddy`).

- [ ] **Step 2: Recreate the SIP trunks + dispatch rule against the live LiveKit**

Follow the existing `infra/README.md` "LiveKit side" + "Outbound calling" steps to create the inbound trunk, dispatch rule, and outbound trunk against the live server; copy the outbound trunk ID into the secret-managed `.env` (push a new Secret Manager version) and redeploy.

- [ ] **Step 3: Register a test elder whose `phone_e164` is YOUR phone**

Run:
```bash
curl -s -X POST https://api.<domain>/v1/elders -H 'content-type: application/json' \
  -d '{"name":"Smoke Test","phone_e164":"+1YOURPHONE","timezone":"America/New_York"}'
```
Expected: `201` with an elder `id`. Record it.

- [ ] **Step 4: Outbound smoke (human-answer path)**

Run:
```bash
python3 scripts/place_test_call.py --elder-id <ELDER_ID> --base-url https://api.<domain> --key live-outbound-1
curl -s https://api.<domain>/v1/calls/<CALL_ID>
```
Expected: your phone rings; on answer you hear the greeting and a check-in; on hangup the call reaches `completed` with `answered_at`/`ended_at`/`duration_seconds` set. Record: did it ring, what you heard, rough first-audio latency.

- [ ] **Step 5: Outbound smoke (voicemail path)**

Place a call (new `--key live-voicemail-1`) and let it go to voicemail (don't answer, or answer with a recorded greeting).
Expected: the call ends `voicemail_left` (the agent's first-3s regex fires, plays the scripted message, hangs up). Record the observed status.

- [ ] **Step 6: Outbound smoke (no-answer path)**

Place a call (`--key live-noanswer-1`) and silence/decline it past the ring timeout.
Expected: status `no_answer`; the retry orchestrator schedules a follow-up `calls` row (`parent_call_id` set, `attempt=2`) subject to quiet hours. Record both rows' statuses.

- [ ] **Step 7: Inbound smoke (known elder)**

Dial the Telnyx DID from the registered phone.
Expected: within ~2s you're greeted **by name**; the agent runs the check-in; a `direction=inbound` call row is created and reaches `completed` with a transcript. Verify:
```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml logs api | grep -i inbound
curl -s https://api.<domain>/v1/calls/<CALL_ID>
```
Record the result.

- [ ] **Step 8: Inbound smoke (unknown number)**

Dial the DID from a number not registered as an elder.
Expected: generic greeting only; a call row with `elder_id: null`. Record the result.

- [ ] **Step 9: Write the results section into `infra/README.md` and commit**

Append a `## Live smoke results (Plan 4a)` section documenting the date, the VM IP/region, and the observed outcome + latency note for each of Steps 4–8 (this is the deliverable the earlier plans' "document the outcome here" notes asked for). Then:

```bash
git add infra/README.md
git commit -m "docs(infra): record Plan 4a live telephony smoke results"
```

---

## Self-Review

**1. Spec coverage (deploy-relevant sections):**
- §4.3 `docker-compose.prod.yml` → Task 5. `docker-compose.tls.yml` → Task 4. `terraform/` single VM + secret-loading-at-boot → Tasks 1–3 (startup.sh pulls from Secret Manager). `.env.example` superset → Task 6 (`.env.prod.example`). ✅
- §10 TLS everywhere (Caddy in front of API + LiveKit signaling) → Task 4; secret management via env at boot, no secrets in code/images → Tasks 2/6 (Secret Manager). ✅ *(SIP/RTP are UDP and not TLS-terminated by Caddy — called out explicitly; SIP-TLS/SRTP is a Telnyx-trunk concern left to a later hardening pass.)*
- §13 `scripts/place_test_call.py` → Task 9. ✅
- §14 `build.yml` (multi-arch GHCR on main) → Task 7; `deploy.yml` (tag → ssh → pull/up → health check) → Task 8. ✅
- Deferred live smoke tests (Plans 1/2a/2b-1/2b-2) → Task 10. ✅
- **Out of Plan-4a scope (correctly):** Egress/recording/GCS/presigned URL (→ Plan 4b), observability/Prometheus/OTel (later), DTMF/reconnection (later), RAG (later), 80% coverage gate + live-audio/voicemail-regression test suites (later — this plan adds CI plumbing, not the coverage gate). Listed so the gap is explicit, not silently dropped.

**2. Placeholder scan:** No "TBD"/"handle errors"/"similar to Task N". Every file has complete contents; every validation step has an exact command + expected output. Domain/project/IP values are intentionally operator-supplied inputs (`<domain>`, `project_id`, `+1YOURPHONE`), documented as such, not plan placeholders.

**3. Type/name consistency:** `usan-prod-env` secret name (Tasks 1/3/6), `GHCR_OWNER`/`IMAGE_TAG`/`API_DOMAIN`/`LIVEKIT_DOMAIN` env keys (Tasks 4/5/6/8), `usan` network tag (Tasks 2/3), and the `-f` overlay ordering (base → prod → tls, Tasks 4/5/8) match across tasks. The `build_request(...)` signature is identical in the test (Task 9 Step 1) and implementation (Step 3). Image names `ghcr.io/<owner>/usan-{api,agent,agent-base}` are consistent across build (Task 7) and prod compose (Task 5).

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-01-plan-4a-deploy-tls.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration. Best here because Tasks 1–9 are independently verifiable (`terraform validate` / `docker compose config` / `actionlint` / `pytest`) without a live cloud account; Task 10 is gated on the operator prerequisites and run interactively.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints for review.

**Which approach?**
