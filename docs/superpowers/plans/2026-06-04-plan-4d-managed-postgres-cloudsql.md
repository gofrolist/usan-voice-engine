# Plan 4d — Managed Postgres on Cloud SQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the production database off the in-VM `pgvector/pgvector:pg18` container onto **GCP Cloud SQL for PostgreSQL** (private IP, regional HA, automated backups + PITR) — closing the current zero-backup/zero-HA gap for elder PHI — while keeping the local container unchanged for dev.

**Architecture:** Cloud SQL is provisioned via Terraform into the **same project (`usan-retirement`) and VPC** as the Plan 4c VM, reachable over **private IP** via Private Services Access (no public exposure, LAN-class latency, free intra-VPC egress, covered by the existing Google Cloud BAA). The app is 100% `DATABASE_URL`-driven, so production becomes a config swap: point `DATABASE_URL` at the Cloud SQL private IP with `?ssl=require`, stop starting the local `postgres` container in prod (a never-enabled compose profile), and let the API's existing entrypoint (`alembic upgrade head`) materialize the schema on Cloud SQL at deploy time. **No application code changes** — asyncpg connects directly to the session-mode private-IP endpoint, so the existing engine config (`db/session.py:19`) and the `FOR UPDATE SKIP LOCKED` retry poller work unmodified.

**Tech Stack:** Terraform (`google` + `random` providers), GCP Cloud SQL for PostgreSQL (Enterprise edition, Postgres 18), Private Services Access / VPC peering, Docker Compose overlays + profiles, SQLAlchemy 2.0 async + asyncpg, Alembic.

---

> **Decision (2026-06-04).** Production DB = **Cloud SQL for PostgreSQL**, chosen over AlloyDB and Supabase. Drivers: it's covered by the single Google Cloud BAA we already sign for Gemini + GCS (no new PHI sub-processor); ~$100–220/mo HA vs AlloyDB ~$520 vs Supabase ~$1,068 HIPAA floor; same-region private IP (<1–3 ms, free egress) vs Supabase's cross-cloud AWS path; and pgvector+HNSW parity with the dev image for the future RAG plan. Dev/test stays on the `pgvector/pgvector:pg18` container.

> **Scope note.** This is the **managed-database slice** of production hardening and the prerequisite for carrying real elder PHI. It is independent of, and does **not** include, the rest of the hardening backlog — observability/ops-agent + alerting, Docker log rotation, container healthchecks, the agent-worker concurrency bound, the daily-call scheduler, consent/opt-out + distress escalation, vendor BAA execution, and the RetellAI cutover plan — which remain for subsequent plans (4e+). Listed so the gap stays explicit.

> **Sequencing.** Execute **after Plan 4c** (VM + VPC + dedicated `usan-vm` service account + first-call validation on the container) and **before carrying real elder traffic**. Cloud SQL reuses the VPC/SA/Secret-Manager scaffolding Plan 4c creates. Tasks 1–2 are pure code (no cloud access); Tasks 3–5 are interactive operator actions against live GCP.

---

## Prerequisites (operator, before Task 1)

1. Plan 4c applied: the `usan-vm` instance, the `default` VPC, the dedicated `google_service_account.vm`, and the `usan-prod-env` Secret Manager secret exist in `usan-retirement`.
2. `gcloud` authenticated to `usan-retirement` (`gcloud config set project usan-retirement`), with permission to enable APIs and create Cloud SQL + Service Networking resources.
3. The Google Cloud BAA is accepted at the org/billing level before real PHI is written (Cloud SQL is a covered service at no surcharge).
4. The filled, gitignored `infra/.env.prod` from Plan 4c in hand (you will edit `DATABASE_URL` in Task 4).

---

## File Structure

**Terraform (`infra/terraform/`):**
- Create `infra/terraform/database.tf` — Cloud SQL instance, DB, user, generated password, Private Services Access, and the `sqladmin`/`servicenetworking` API enables.
- Modify `infra/terraform/versions.tf` — add the `hashicorp/random` provider.
- Modify `infra/terraform/variables.tf` — add `db_tier`, `db_availability_type`, `db_disk_gb`.
- Modify `infra/terraform/outputs.tf` — add `db_private_ip`, `db_connection_name`, `db_password` (sensitive).

**Compose + env (`infra/`):**
- Modify `infra/docker-compose.prod.yml` — exclude the local `postgres` container in prod (never-enabled profile) and drop the API's `postgres` dependency.
- Modify `infra/.env.prod.example` — document the Cloud SQL `DATABASE_URL` form.

No `apps/` changes.

---

## Task 1: Terraform — Cloud SQL instance, private networking, user

**Files:**
- Create: `infra/terraform/database.tf`
- Modify: `infra/terraform/versions.tf`
- Modify: `infra/terraform/variables.tf`
- Modify: `infra/terraform/outputs.tf`

- [ ] **Step 1: Add the `random` provider to `infra/terraform/versions.tf`**

Add the `random` entry inside `required_providers` (alongside the existing `google` block):

```hcl
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
```

- [ ] **Step 2: Append the DB variables to `infra/terraform/variables.tf`**

```hcl
variable "db_tier" {
  type        = string
  description = "Cloud SQL machine tier (Enterprise edition). db-custom-1-3840 (1 vCPU / 3.75GB) is the cheap baseline for 5k-50k calls/mo; bump to db-custom-2-7680 if needed."
  default     = "db-custom-1-3840"
}

variable "db_availability_type" {
  type        = string
  description = "REGIONAL = synchronous HA standby in a 2nd zone (recommended for prod PHI); ZONAL = single zone, ~half the cost, no automatic failover."
  default     = "REGIONAL"
}

variable "db_disk_gb" {
  type        = number
  description = "Cloud SQL data disk size in GB (autoresizes upward from here)."
  default     = 20
}
```

- [ ] **Step 3: Write `infra/terraform/database.tf`**

```hcl
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
      start_time                     = "08:00" # UTC, off-peak for US elders
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
```

- [ ] **Step 4: Append the DB outputs to `infra/terraform/outputs.tf`**

```hcl
output "db_private_ip" {
  description = "Cloud SQL private IP. Prod DATABASE_URL = postgresql://usan:<db_password>@<db_private_ip>:5432/usan?ssl=require"
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
```

- [ ] **Step 5: Validate formatting + syntax**

Run: `cd infra/terraform && terraform fmt -check && terraform init -backend=false && terraform validate`
Expected: `fmt -check` exit 0; `init` downloads the `random` provider; `validate` → `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infra/terraform/database.tf infra/terraform/versions.tf infra/terraform/variables.tf infra/terraform/outputs.tf
git commit -m "infra: provision Cloud SQL for Postgres (private IP, HA, PITR)"
```

---

## Task 2: Compose + env wiring — externalize the prod DB

**Files:**
- Modify: `infra/docker-compose.prod.yml`
- Modify: `infra/.env.prod.example`

In prod the API must talk to Cloud SQL (`DATABASE_URL`), not the local container. The base file's `postgres` service and the API's `depends_on: postgres` must be neutralized **only in prod** — dev (base file alone) keeps the container.

- [ ] **Step 1: Exclude `postgres` and drop the API dependency in `infra/docker-compose.prod.yml`**

Add a `depends_on: !reset null` to the existing `api` service block, and add a new `postgres` block with a never-enabled profile. The `api` block becomes:

```yaml
  api:
    image: ghcr.io/${GHCR_OWNER}/usan-api:${IMAGE_TAG:?IMAGE_TAG must be set to an explicit tag}
    pull_policy: always
    build: !reset null
    # Prod talks to Cloud SQL via DATABASE_URL (private IP), not the local
    # container, so drop the base file's `depends_on: postgres`.
    depends_on: !reset null
    # API is reached by Caddy over the compose network as api:8000.
    # Keep a loopback publish so on-VM `curl localhost:8000/health` works.
    ports: !override
      - "127.0.0.1:8000:8000"
```

And append a `postgres` block (sibling to the other services in the overlay):

```yaml
  # Production uses Cloud SQL (Plan 4d), not the in-VM container. The `localdb`
  # profile is never enabled in prod, so this service does not start here. Dev
  # (base file, no overlay) has no profile on postgres and still runs it.
  postgres:
    profiles: ["localdb"]
```

- [ ] **Step 2: Document the Cloud SQL `DATABASE_URL` in `infra/.env.prod.example`**

Replace the Postgres block in `infra/.env.prod.example` with:

```bash
# === Postgres ===
# Prod (Plan 4d): point DATABASE_URL at the Cloud SQL PRIVATE IP. Get the values from
#   terraform output db_private_ip   and   terraform output -raw db_password
#   DATABASE_URL=postgresql://usan:<db_password>@<db_private_ip>:5432/usan?ssl=require
# The POSTGRES_* vars below are only consumed by the LOCAL dev container, which is
# NOT started in prod (it sits behind the `localdb` compose profile).
POSTGRES_USER=usan
POSTGRES_PASSWORD=__STRONG_RANDOM__
POSTGRES_DB=usan
DATABASE_URL=postgresql://usan:__CLOUD_SQL_PASSWORD__@__CLOUD_SQL_PRIVATE_IP__:5432/usan?ssl=require
```

- [ ] **Step 3: Validate the prod render excludes postgres and the API has no DB dependency**

Run (throwaway env so substitution succeeds):
```bash
GHCR_OWNER=gofrolist IMAGE_TAG=v0.1.0 API_DOMAIN=api.usanretirement.com LIVEKIT_DOMAIN=lk.usanretirement.com \
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file infra/.env.example config --services
```
Expected: the service list does **NOT** include `postgres` (it's gated behind the inactive `localdb` profile), and `api`, `agent`, `redis`, `livekit`, `livekit-sip`, `egress` are present.

Then confirm the API has no postgres dependency in the prod render:
```bash
GHCR_OWNER=gofrolist IMAGE_TAG=v0.1.0 API_DOMAIN=api.usanretirement.com LIVEKIT_DOMAIN=lk.usanretirement.com \
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml --env-file infra/.env.example config | grep -A3 -E "^  api:" | grep -i depends_on || echo "api has no depends_on in prod (correct)"
```
Expected: prints `api has no depends_on in prod (correct)`.

And confirm dev still runs postgres (base file alone):
```bash
docker compose -f infra/docker-compose.yml --env-file infra/.env.example config --services | grep -q postgres && echo "dev still has postgres (correct)"
```
Expected: `dev still has postgres (correct)`.

- [ ] **Step 4: Commit**

```bash
git add infra/docker-compose.prod.yml infra/.env.prod.example
git commit -m "infra: use Cloud SQL in prod (profile out local postgres, drop api db dep)"
```

---

## Task 3: Provision Cloud SQL (`terraform apply`)

**Files:** none (operator action).

- [ ] **Step 1: Plan**

Run: `cd infra/terraform && terraform init && terraform plan -out=tfplan`
Expected: adds the global address, service-networking connection, two `google_project_service` enables, the `random_password`, the `google_sql_database_instance`, `google_sql_database`, and `google_sql_user` — **no** changes that destroy the VM or bucket. (`tfplan` is gitignored.)

- [ ] **Step 2: Apply (instance creation takes ~10–20 min)**

Run: `terraform apply tfplan`
Expected: `Apply complete!`; the Cloud SQL instance `usan-pg` is created with regional HA.

- [ ] **Step 3: Capture the connection values**

```bash
terraform output db_private_ip
terraform output -raw db_password; echo
terraform output db_connection_name
```
Expected: a private RFC1918 IP, the generated password, and `usan-retirement:us-east1:usan-pg`. Record the IP + password for Task 4.

- [ ] **Step 4: Confirm the instance, HA, and backups**

```bash
gcloud sql instances describe usan-pg --project=usan-retirement \
  --format="table(state, settings.availabilityType, settings.backupConfiguration.enabled, settings.backupConfiguration.pointInTimeRecoveryEnabled, settings.ipConfiguration.ipv4Enabled)"
```
Expected: `state=RUNNABLE`, `availabilityType=REGIONAL`, backups `True`, PITR `True`, `ipv4Enabled=False`.

---

## Task 4: Point production at Cloud SQL and migrate

**Files:** `infra/.env.prod` (gitignored — local edit only).

- [ ] **Step 1: Set `DATABASE_URL` in `infra/.env.prod` to the Cloud SQL private IP**

Using the Task 3 values:

```bash
DATABASE_URL=postgresql://usan:<db_password>@<db_private_ip>:5432/usan?ssl=require
```
Keep `?ssl=require` — `settings.py:127-136` warns (PHI may transit unencrypted) without it, and the instance is `ENCRYPTED_ONLY`. The app derives the `postgresql+asyncpg://` driver itself (`settings.py:93-101`). **Use `?ssl=require`, NOT `?sslmode=require`** — asyncpg uses the `ssl` param and rejects libpq's `sslmode` (`TypeError: connect() got an unexpected keyword argument 'sslmode'`).

- [ ] **Step 2: Push the updated `.env.prod` as a new secret version**

```bash
gcloud secrets versions add usan-prod-env --data-file=infra/.env.prod --project=usan-retirement
```
Expected: `Created version [N] of the secret [usan-prod-env].`

- [ ] **Step 3: Refresh `.env` on the VM (re-run the boot fetch)**

```bash
gcloud compute instances reset usan-vm --zone=us-east1-b --project=usan-retirement
```
Wait ~60–90s; confirm the new DATABASE_URL landed:
```bash
gcloud compute ssh usan@usan-vm --zone=us-east1-b --project=usan-retirement \
  --command="sudo grep -c 'ssl=require' /opt/usan/infra/.env"
```
Expected: `1`.

- [ ] **Step 4: Redeploy so the API entrypoint runs `alembic upgrade head` against Cloud SQL**

Cut a new release tag (preferred — same flow as Plan 4c Task 8) so build+deploy ships and restarts the stack:
```bash
git tag v0.2.0 && git push origin v0.2.0
```
(Set `IMAGE_TAG=v0.2.0` in `.env.prod` + re-push the secret first if you pin it there.) `apps/api/docker-entrypoint.sh` runs `alembic upgrade head` on container start, creating the schema (and `CREATE EXTENSION pgcrypto`, which the cloudsqlsuperuser `usan` role is permitted to do) on Cloud SQL.

- [ ] **Step 5: Verify migrations applied on Cloud SQL**

```bash
ssh usan@<vm_external_ip> 'cd /opt/usan && docker compose --env-file infra/.env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml logs api | grep -E "Running database migrations|Starting API server"'
```
Expected: both log lines present and no Alembic/connection error. If the entrypoint migration failed, run it manually:
```bash
ssh usan@<vm_external_ip> 'cd /opt/usan && docker compose --env-file infra/.env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml run --rm api alembic upgrade head'
```

---

## Task 5: Verify end to end + confirm no local DB in prod

**Files:** none.

- [ ] **Step 1: API healthy against the managed DB**

Run: `curl -fsS https://api.usanretirement.com/health`
Expected: `{"status":"ok"}` (the app is up and connected to Cloud SQL).

- [ ] **Step 2: Confirm the local `postgres` container is NOT running in prod**

```bash
ssh usan@<vm_external_ip> 'cd /opt/usan && docker compose --env-file infra/.env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml ps --services'
```
Expected: lists `api`, `agent`, `redis`, `livekit`, `livekit-sip`, `egress`, `caddy` — **no** `postgres`.

- [ ] **Step 3: Round-trip a write/read through the API (proves Cloud SQL persistence)**

```bash
curl -s -X POST https://api.usanretirement.com/v1/elders \
  -H "content-type: application/json" -H "Authorization: Bearer $OPERATOR_API_KEY" \
  -d '{"name":"DB Migration Check","phone_e164":"+15555550100","timezone":"America/New_York"}'
```
Expected: `201` with an elder `id`. Then confirm it's in Cloud SQL:
```bash
gcloud sql connect usan-pg --user=usan --project=usan-retirement --database=usan \
  --quiet <<< "select count(*) from elders;"
```
Expected: a count ≥ 1. (Delete the check row afterward if desired.)

- [ ] **Step 4: Re-run a live call smoke (Plan 4c Task 9 Step 2) to confirm the pipeline still works on the managed DB**

Place one outbound test call to your phone and confirm the `calls` row reaches `completed` (now persisted in Cloud SQL).

- [ ] **Step 5: Record the migration in `infra/README.md` and commit**

Append a `## Database: Cloud SQL (Plan 4d)` note documenting the instance name, tier, HA/backup settings, and the date the prod cutover happened. Then:

```bash
git add infra/README.md
git commit -m "docs(infra): record Cloud SQL production database cutover (Plan 4d)"
```

---

## Self-Review

**1. Coverage:**
- Managed reliability (backups + PITR + regional HA) replacing the zero-backup container → Task 1 (`backup_configuration` + `availability_type=REGIONAL`). ✅
- Private, BAA-covered, low-latency connectivity → Task 1 (`ipv4_enabled=false`, PSA peering on the `default` VPC). ✅
- No app code change (DATABASE_URL-only) → Task 4; relies on `settings.py:93-101` driver derivation and a direct session-mode connection (no transaction pooler, so `db/session.py:19` is fine as-is). ✅
- Stop running the in-VM DB in prod, keep it in dev → Task 2 (`postgres` profile `localdb` + `api depends_on: !reset null`); dev render still includes postgres (Task 2 Step 3). ✅
- Migrations + pgcrypto extension on Cloud SQL → Task 4 (entrypoint `alembic upgrade head`; cloudsqlsuperuser `usan` user can CREATE EXTENSION). ✅
- **Correctly out of scope** (subsequent plans): observability/alerting, log rotation, healthchecks, worker concurrency, daily-call scheduler, consent/escalation, BAA execution, RetellAI cutover. ✅

**2. Placeholder scan:** No "TBD"/"handle errors"/"similar to Task N". `<db_password>`, `<db_private_ip>`, `<vm_external_ip>`, `$OPERATOR_API_KEY`, `v0.2.0` are operator-supplied/Terraform-output values, documented as such. Every step has an exact command + expected output.

**3. Consistency:** Instance `usan-pg`, DB/user `usan`, region `us-east1`, zone `us-east1-b`, secret `usan-prod-env`, VM `usan-vm`, connection name `usan-retirement:us-east1:usan-pg` match across tasks. `DATABASE_URL` uses `?ssl=require` everywhere, matching `settings.py:127`'s TLS check. The `random` provider added in versions.tf (Task 1 Step 1) backs `random_password.db` used in database.tf (Step 3) and the `db_password` output (Step 4). `Authorization: Bearer` auth matches the corrected Plan 4c.

**Notes / caveats baked in:**
- `deletion_protection = true` blocks `terraform destroy` of the instance; to tear down (non-prod only) set it `false` + apply first. The `google_service_networking_connection` also resists deletion while the instance uses the range — destroy the instance first.
- This is the **direct-private-IP + password** path (zero code change, password lives only in Secret Manager). A keyless **IAM-auth via Cloud SQL Auth Proxy** upgrade (matching the GCS signBlob/ADC model) is a worthwhile follow-up but adds a proxy sidecar + IAM DB user + privilege grants — deferred, not in this plan.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-04-plan-4d-managed-postgres-cloudsql.md`. Two execution options:**

**1. Subagent-Driven (recommended for Tasks 1–2)** — fresh subagent per task; the Terraform + compose edits are independently verifiable (`terraform fmt`/`validate`, `docker compose config`) without cloud access.

**2. Inline / interactive (required for Tasks 3–5)** — one-time operator actions that create a billed Cloud SQL instance, rotate the prod secret, redeploy, and place a real call; run interactively in this session.

**Which approach?**
