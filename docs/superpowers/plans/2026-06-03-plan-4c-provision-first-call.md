# Plan 4c — Provision GCP & First Real Call Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand the (already-built, already-merged) stack up on a real GCP VM in project `usan-retirement` and place the first real outbound + inbound calls — i.e. finally *execute* the provisioning and live-smoke validation that Plan 4a defined as code but never ran, after clearing the two `terraform apply` blockers that currently halt it.

**Architecture:** Plans 1–4b shipped all the code (Terraform, compose overlays, Caddy TLS, CI build+deploy, agent pipeline, egress→GCS). Nothing has ever been provisioned: `gcloud compute instances list --project=usan-retirement` is empty and no `tfstate` exists. This plan is a **provisioning runbook plus two small Terraform fixes**. First it unblocks a non-interactive `terraform apply` (delete the dead `image_tag` variable; add the required `telnyx_sip_signaling_source_ranges` to `terraform.tfvars`). Then it walks the one-time operator sequence in dependency order: apply → push the prod `.env` to Secret Manager → reset the VM so the boot script materializes `.env` → DNS → GitHub deploy secrets → point Telnyx at the static IP → cut a release tag (fires `build.yml`'s `deploy` job) → place real test calls and validate the live SIP/voicemail classifiers.

**Tech Stack:** Terraform (`google` provider, local state), GCP Compute Engine + Secret Manager + Cloud Storage + IAM, `gcloud` CLI, GitHub Actions (`build.yml` build+deploy, tag-triggered), Docker Compose overlays on the VM, Caddy 2 (Let's Encrypt), `scripts/place_test_call.py`.

---

> **Scope note.** Plan 4c gets the system to its **first real call** on real infrastructure. It is deliberately **not** the production-hardening plan. The following are explicitly **deferred to a later "Plan 4d — Production Hardening"** and must land before carrying real elder PHI at volume (they are listed here so the gap is explicit, not silently dropped):
> - Postgres backups (`pg_dump`/snapshot → versioned GCS) + restore runbook.
> - Observability: install `google-cloud-ops-agent` in `startup.sh` (the VM SA already has `logging.logWriter` + `monitoring.metricWriter` but they are inert), Cloud Monitoring alert policies (API health, agent-worker liveness, disk, call-failure rate), Prometheus/OTel in the apps.
> - Docker log rotation on the 30 GB boot disk; container healthchecks + a liveness probe for the agent worker.
> - Agent-worker concurrency bound (`WorkerOptions` in `services/agent/.../worker.py`) and single-VM capacity load-test for the 5k–50k calls/month morning surge.
> - The **daily-call scheduler / batch trigger** (does not exist in the repo or any plan; `enqueue_call` is external-only — confirm its owner before the RetellAI cutover).
> - Consent-vs-disclosure (per-elder opt-out, two-party-consent states), in-call distress/emergency escalation, vendor BAAs, and the RetellAI cutover/parallel-run/rollback plan.

> **Dependency note (do tasks roughly in order).** Task 1 must precede Task 3 (apply will otherwise halt on the missing variable). Task 4 (secret version + VM reset) must precede Task 8 (deploy reads `/opt/usan/infra/.env`). Task 5 (DNS) must precede Task 8's health gate (Caddy needs live DNS to issue the cert and the gate curls `https://api.<domain>/health`). Tasks 5/6/7 can be done in parallel after Task 3.

---

## Prerequisites (operator, before Task 1)

One-time setups the engineer needs in hand. Not code tasks, but the plan cannot complete without them:

1. `gcloud` authenticated and pointed at the project:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project usan-retirement
   ```
2. Billing/paid tier enabled on the project AND on the `GEMINI_API_KEY`'s Google project. *(Per the `gemini-billing-blocker` note this was resolved 2026-06-03 — credits added, `gemini-3.1-flash-lite` completes a full local smoke call. Confirm it is still active; if billing ever lapses the agent LLM 403s and no conversation can occur.)*
3. Control of DNS for `usanretirement.com` (ability to create A records `api.` and `lk.`).
4. A **filled** `infra/.env.prod` in hand. *(One already exists locally with `GCS_BUCKET=usan-retirement-call-recordings`; it is gitignored. You will edit `IMAGE_TAG` in Task 4 and push it to Secret Manager — never commit it.)*
5. Admin on the GitHub repo to add Actions secrets.
6. Telnyx SIP trunk credentials (already in `.env.prod`) and the inbound DID; you will point the trunk's signaling at the VM IP in Task 7.
7. A deploy SSH keypair whose **public** half is already in `terraform.tfvars` (`ssh_public_key = "ssh-ed25519 AAAA…C3…HIr usan-deploy"`); have the matching **private** key for the GitHub `DEPLOY_SSH_KEY` secret (Task 6).

---

## File Structure

Only two committed files change (plus the gitignored, local-only `terraform.tfvars`); everything else is operator action against GCP/GitHub/DNS/Telnyx:

**Terraform (`infra/terraform/`):**
- Modify `infra/terraform/variables.tf` — delete the dead `image_tag` variable.
- Modify `infra/terraform/terraform.tfvars.example` — remove the stale `# image_tag` line, add a `telnyx_sip_signaling_source_ranges` template line, and align the example `recordings_bucket` comment.
- Modify `infra/terraform/terraform.tfvars` (**gitignored — local only, not committed**) — add `telnyx_sip_signaling_source_ranges`.
- *(Optional, Task 2)* Modify `infra/terraform/versions.tf` — add a GCS remote-state backend.

**Docs:**
- Modify `infra/README.md` — add a "Live smoke results (Plan 4c)" results section (written in Task 9 Step 6).

No application code changes.

---

## Task 1: Unblock `terraform apply` (delete dead `image_tag`, add Telnyx SIP CIDRs)

**Files:**
- Modify: `infra/terraform/variables.tf` (delete `image_tag` block, lines 66–69)
- Modify: `infra/terraform/terraform.tfvars.example`
- Modify: `infra/terraform/terraform.tfvars` (gitignored — local only)

`terraform apply` against `usan-retirement` halts today on two required-but-unset variables: `telnyx_sip_signaling_source_ranges` (consumed by `google_compute_firewall.sip` in `main.tf:138-148`) and `image_tag` (declared at `variables.tf:66`, **referenced by no resource** — the VM gets its tag from the deploy workflow's `IMAGE_TAG`, not Terraform). Fix: supply the first, delete the second.

- [ ] **Step 1: Prove `image_tag` is dead before deleting it**

Run: `grep -rn "image_tag" infra/ .github/ services/ apps/ scripts/`
Expected: exactly two hits — `infra/terraform/variables.tf:66` (the declaration) and `infra/terraform/terraform.tfvars.example:8` (a comment). No resource, `startup.sh`, or workflow references it. (If any *other* hit appears, stop — it is not dead; set it instead of deleting.)

- [ ] **Step 2: Delete the `image_tag` variable from `infra/terraform/variables.tf`**

Remove this block (currently lines 66–69):

```hcl
variable "image_tag" {
  type        = string
  description = "Container image tag the VM should pull on first boot (passed into the startup script). Must be an explicit immutable tag; no 'latest' fallback."
}
```

- [ ] **Step 3: Update `infra/terraform/terraform.tfvars.example`**

Replace the file contents with (drops the stale `# image_tag` line, adds the Telnyx-CIDR template, clarifies the bucket comment):

```hcl
# Copy to terraform.tfvars and fill in. terraform.tfvars is gitignored.
project_id        = "your-gcp-project-id"
region            = "us-east1"
zone              = "us-east1-b"
ssh_user          = "usan"
ssh_public_key    = "ssh-ed25519 AAAA... you@host"
operator_ssh_cidr = "203.0.113.4/32" # your workstation IP /32
recordings_bucket = "your-globally-unique-bucket" # e.g. usan-retirement-call-recordings

# REQUIRED: Telnyx's CURRENT published SIP signaling CIDRs (verify at https://sip.telnyx.com).
# Wrong/stale values silently break inbound calls. As of 2026-06:
telnyx_sip_signaling_source_ranges = [
  "36.255.198.128/25", "50.114.136.128/25", "50.114.144.0/21",
  "64.16.226.0/24", "64.16.227.0/24", "64.16.228.0/24", "64.16.229.0/24",
  "64.16.230.0/24", "64.16.248.0/24", "64.16.249.0/24",
  "103.115.244.128/25", "103.115.247.128/27",
  "185.246.41.128/25", "185.246.42.128/28",
]

# recording_nearline_days  = 30
# recording_retention_days = 365
```

- [ ] **Step 4: Add `telnyx_sip_signaling_source_ranges` to the real `infra/terraform/terraform.tfvars` (local, gitignored)**

Append this block to `infra/terraform/terraform.tfvars` (which already sets `project_id`, `region`, `zone`, `ssh_user`, `ssh_public_key`, `operator_ssh_cidr`, `recordings_bucket`):

```hcl
telnyx_sip_signaling_source_ranges = [
  "36.255.198.128/25", "50.114.136.128/25", "50.114.144.0/21",
  "64.16.226.0/24", "64.16.227.0/24", "64.16.228.0/24", "64.16.229.0/24",
  "64.16.230.0/24", "64.16.248.0/24", "64.16.249.0/24",
  "103.115.244.128/25", "103.115.247.128/27",
  "185.246.41.128/25", "185.246.42.128/28",
]
```

> Re-verify the list against https://sip.telnyx.com before applying — Telnyx rotates/expands these. A stale CIDR silently drops inbound SIP.

- [ ] **Step 5: Validate Terraform formatting + syntax**

Run: `cd infra/terraform && terraform fmt -check && terraform init -backend=false && terraform validate`
Expected: `fmt -check` prints nothing (exit 0); `validate` → `Success! The configuration is valid.`

> Note: `terraform validate` does **not** check that required variables have values — the real proof that the blocker is cleared is `terraform plan` in Task 3 Step 2, which must NOT report `No value for required variable`.

- [ ] **Step 6: Commit (committed files only — terraform.tfvars stays local)**

```bash
git add infra/terraform/variables.tf infra/terraform/terraform.tfvars.example
git commit -m "infra: drop dead image_tag var, document Telnyx SIP CIDRs in tfvars example"
```

---

## Task 2 (optional, recommended): GCS remote state backend

**Files:**
- Modify: `infra/terraform/versions.tf`

State is currently local and gitignored. For a single-VM production deploy, losing the workstation means losing the ability to cleanly manage/destroy the infra. A GCS backend makes state durable and shareable. **Skippable for the very first apply**, but do it before treating the deploy as durable. If you skip, jump to Task 3.

- [ ] **Step 1: Create a state bucket (versioned)**

```bash
gcloud storage buckets create gs://usan-retirement-tfstate \
  --project=usan-retirement --location=us-east1 --uniform-bucket-level-access
gcloud storage buckets update gs://usan-retirement-tfstate --versioning
```
Expected: bucket created; versioning enabled. (If the global name is taken, choose another and use it below.)

- [ ] **Step 2: Add the backend block to `infra/terraform/versions.tf`**

Insert inside the existing `terraform { ... }` block, after `required_providers { ... }`:

```hcl
  backend "gcs" {
    bucket = "usan-retirement-tfstate"
    prefix = "usan-vm"
  }
```

- [ ] **Step 3: Migrate local state to the backend**

Run: `cd infra/terraform && terraform init -migrate-state`
Expected: prompts to copy existing state to the new `gcs` backend → answer `yes`. (On a fresh tree with no prior state, it simply initializes the backend.)

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/versions.tf
git commit -m "infra: use GCS remote backend for terraform state durability"
```

---

## Task 3: Provision GCP (`terraform apply`)

**Files:** none (operator action; reads `terraform.tfvars` from Task 1).

Creates: static IP, the `usan-vm` Compute Engine instance, the dedicated least-privilege `usan-vm` service account + IAM (secret accessor, log/metric writer, GCS objectViewer+objectCreator, signBlob tokenCreator), the firewall rules (`ssh`/`web`/`media`/`sip`), the `usan-prod-env` Secret Manager **container** (no version yet), the recordings GCS bucket with lifecycle, and enables `iamcredentials.googleapis.com`.

- [ ] **Step 1: Confirm the target project and that no VM exists yet**

```bash
gcloud config get-value project          # -> usan-retirement
gcloud compute instances list --project=usan-retirement
```
Expected: project is `usan-retirement`; instance list is empty (first-time provision).

- [ ] **Step 2: Plan (this is the real "blocker cleared" assertion)**

Run: `cd infra/terraform && terraform init && terraform plan -out=tfplan` (`tfplan` is gitignored)
Expected: a plan that will **create** ~15 resources, and **no** `No value for required variable` error for `telnyx_sip_signaling_source_ranges` or `image_tag`. If you see that error, return to Task 1.

- [ ] **Step 3: Apply**

Run: `terraform apply tfplan`
Expected: `Apply complete!` with the `google_compute_instance.usan`, `google_compute_address.usan`, `google_secret_manager_secret.env`, `google_storage_bucket.recordings`, the four firewalls, and the SA/IAM resources created.

- [ ] **Step 4: Capture the outputs you will need downstream**

```bash
terraform output vm_external_ip       # -> use for DNS (Task 5), GitHub DEPLOY_HOST (Task 6), Telnyx (Task 7)
terraform output recordings_bucket     # -> must equal GCS_BUCKET in .env.prod (Task 4)
terraform output secret_name           # -> usan-prod-env
terraform output ssh_command
```
Expected: a static IPv4, `usan-retirement-call-recordings`, `usan-prod-env`. Record the IP.

- [ ] **Step 5: Verify the VM exists and firewalls are present**

```bash
gcloud compute instances list --project=usan-retirement
gcloud compute firewall-rules list --project=usan-retirement \
  --filter="name~usan" --format="table(name,allowed[].ports.flatten())"
```
Expected: `usan-vm` is `RUNNING`; rules `usan-allow-ssh`, `usan-allow-web`, `usan-allow-media`, `usan-allow-sip` listed with the expected ports (22; 80/443 + 443/udp; 10000-20000 + 50000-60000/udp; 5060/udp).

---

## Task 4: Populate Secret Manager and materialize `.env` on the VM

**Files:** `infra/.env.prod` (gitignored — local edit only, never committed).

The VM's `startup.sh` runs `gcloud secrets versions access latest --secret=usan-prod-env > /opt/usan/infra/.env` on every boot. At Task 3 apply time the secret had **no version**, so the first boot's fetch failed (under `set -euo pipefail`) and `.env` was not written — expected. Push a version, then reset the VM so the boot script re-runs and materializes `.env`.

- [ ] **Step 1: Set an explicit immutable `IMAGE_TAG` in `infra/.env.prod`**

Edit `infra/.env.prod`: set `IMAGE_TAG` to the exact release tag you will push in Task 8 (do **not** use `latest` — the prod compose uses `${IMAGE_TAG:?...}` and the project requires an immutable tag):

```bash
IMAGE_TAG=v0.1.0
```

- [ ] **Step 2: Confirm `.env.prod` is internally consistent with the provisioned infra**

Run: `grep -E '^(GCS_BUCKET|API_DOMAIN|LIVEKIT_DOMAIN|GHCR_OWNER|IMAGE_TAG)=' infra/.env.prod`
Expected: `GCS_BUCKET=usan-retirement-call-recordings` (matches `terraform output recordings_bucket`), `API_DOMAIN=api.usanretirement.com`, `LIVEKIT_DOMAIN=lk.usanretirement.com`, `GHCR_OWNER=gofrolist`, `IMAGE_TAG=v0.1.0`. Fix any mismatch before pushing.

- [ ] **Step 3: Push the filled `.env.prod` as a secret version**

```bash
gcloud secrets versions add usan-prod-env \
  --data-file=infra/.env.prod --project=usan-retirement
```
Expected: `Created version [1] of the secret [usan-prod-env].`

- [ ] **Step 4: Verify the version is readable**

Run: `gcloud secrets versions access latest --secret=usan-prod-env --project=usan-retirement | grep -E '^(IMAGE_TAG|GCS_BUCKET)='`
Expected: prints the `IMAGE_TAG=v0.1.0` and `GCS_BUCKET=...` lines (proves the SA path the VM uses will succeed).

- [ ] **Step 5: Reset the VM so `startup.sh` re-runs and writes `/opt/usan/infra/.env`**

```bash
gcloud compute instances reset usan-vm --zone=us-east1-b --project=usan-retirement
```
Wait ~60–90s for boot, then confirm `.env` materialized:
```bash
gcloud compute ssh usan@usan-vm --zone=us-east1-b --project=usan-retirement \
  --command='sudo test -s /opt/usan/infra/.env && echo ENV_OK && docker --version'
```
Expected: `ENV_OK` and a Docker version string (proves both the secret fetch and the Docker install from `startup.sh`).

---

## Task 5: DNS A records → static IP

**Files:** none (operator action at the DNS provider).

Caddy obtains Let's Encrypt certs only if the domains resolve to the VM, and Task 8's health gate curls `https://api.usanretirement.com/health`.

- [ ] **Step 1: Create A records at your DNS provider**

Point both at the `vm_external_ip` from Task 3 Step 4:
- `api.usanretirement.com` → `<vm_external_ip>`
- `lk.usanretirement.com`  → `<vm_external_ip>`

- [ ] **Step 2: Verify propagation before deploying**

```bash
dig +short api.usanretirement.com
dig +short lk.usanretirement.com
```
Expected: both return the `<vm_external_ip>`. (Wait and re-check if empty; do not proceed to Task 8 until they resolve.)

---

## Task 6: GitHub Actions deploy secrets

**Files:** none (operator action in GitHub repo settings; consumed by `build.yml`'s `deploy` job).

The `deploy` job (`.github/workflows/build.yml:78-133`) reads `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `API_DOMAIN`, and `GHCR_PAT`.

- [ ] **Step 1: Set the five repo secrets**

```bash
gh secret set DEPLOY_HOST   --body "<vm_external_ip>"
gh secret set DEPLOY_USER   --body "usan"
gh secret set DEPLOY_SSH_KEY < /path/to/usan-deploy-private-key   # via stdin — keeps the key out of shell history
gh secret set API_DOMAIN    --body "api.usanretirement.com"
gh secret set GHCR_PAT      --body "<a PAT with read:packages>"
```
> `DEPLOY_SSH_KEY` is the **private** half of the `ssh_public_key` in `terraform.tfvars`. `GHCR_PAT` lets the VM `docker login ghcr.io` to pull the private `usan-*` images.

- [ ] **Step 2: Verify the secrets exist**

Run: `gh secret list`
Expected: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `API_DOMAIN`, `GHCR_PAT` all listed.

- [ ] **Step 3: Smoke-test SSH from your workstation (catch key/firewall issues early)**

Run: `ssh -o StrictHostKeyChecking=accept-new usan@<vm_external_ip> 'echo SSH_OK'`
Expected: `SSH_OK`. (If it hangs, confirm your workstation IP matches `operator_ssh_cidr` in `terraform.tfvars`.)

---

## Task 7: Point Telnyx at the VM and recreate LiveKit SIP trunks/dispatch

**Files:** none (operator action; uses `infra/livekit-sip-*.json` + `infra/README.md`).

- [ ] **Step 1: Point the Telnyx trunk's inbound SIP signaling at the VM**

In the Telnyx portal, set the SIP connection's signaling destination to `<vm_external_ip>:5060` (UDP). Confirm the trunk's outbound credentials match `TELNYX_SIP_USERNAME`/`TELNYX_SIP_PASSWORD` in `.env.prod`.

- [ ] **Step 2: Recreate the inbound trunk + dispatch rule and outbound trunk against the LIVE LiveKit**

Follow the existing `infra/README.md` "LiveKit side" / "Outbound calling" steps using `infra/livekit-sip-trunk.json`, `infra/livekit-sip-dispatch-rule.json`, and `infra/livekit-sip-outbound-trunk.json` against the deployed server (`wss://lk.usanretirement.com`). The outbound trunk auto-provisions on the first dial from the Telnyx creds (`LIVEKIT_SIP_OUTBOUND_TRUNK_ID` is left blank per `.env.prod.example:37-39`); only pin an `ST_...` ID to override.

- [ ] **Step 3: Verify the inbound trunk + dispatch rule exist**

```bash
lk sip inbound list   # against the live server (LIVEKIT_URL/keys for prod)
lk sip dispatch list
```
Expected: the inbound trunk and a dispatch rule routing to `AGENT_NAME=usan-agent` are present.

---

## Task 8: Cut a release tag → build + deploy → health gate

**Files:** none (triggers `build.yml`).

Pushing a `v*` tag builds the three images to GHCR and, via `needs: build` + `if: github.ref_type == 'tag'`, scp's the compose files to the VM, `docker login ghcr.io`, `docker compose ... pull && up -d`, then polls `https://api.usanretirement.com/health`. The API entrypoint runs `alembic upgrade head` on start, so migrations need no separate step.

- [ ] **Step 1: Tag and push (tag must match the `IMAGE_TAG` from Task 4)**

```bash
git tag v0.1.0
git push origin v0.1.0
```

- [ ] **Step 2: Watch the run to green**

Run: `gh run watch "$(gh run list --workflow=build.yml --branch v0.1.0 --limit 1 --json databaseId -q '.[0].databaseId')"`
Expected: both `build` and `deploy` jobs succeed; the "Post-deploy health check" step prints `healthy`.

- [ ] **Step 3: Independently verify the live stack**

```bash
curl -fsS https://api.usanretirement.com/health
ssh usan@<vm_external_ip> 'cd /opt/usan && docker compose --env-file infra/.env -f infra/docker-compose.yml -f infra/docker-compose.prod.yml -f infra/docker-compose.tls.yml ps'
```
Expected: `{"status":"ok"}` over real TLS; all containers (`api`, `agent`, `livekit`, `livekit-sip`, `postgres`, `redis`, `caddy`) are `Up`. If health fails, `ssh` in and check `docker compose ... logs caddy` (cert issuance needs Task 5's DNS live) and `... logs api`.

---

## Task 9: First real calls + live classifier validation

**Files:**
- Modify: `infra/README.md` (append a "## Live smoke results (Plan 4c)" section as you go)

This is the payoff and the validation deferred across Plans 1/2a/2b/4a (4a's Task 10 could never run — no VM). Record each observed outcome so the evidence is durable, and confirm the SIP-failure and voicemail classifiers match the **live** exception/transcript shapes (both carry explicit "verify against live" caveats).

- [ ] **Step 1: Register a test elder whose `phone_e164` is YOUR phone**

```bash
curl -s -X POST https://api.usanretirement.com/v1/elders \
  -H "content-type: application/json" \
  -H "Authorization: Bearer $OPERATOR_API_KEY" \
  -d '{"name":"Smoke Test","phone_e164":"+1YOURPHONE","timezone":"America/New_York"}'
```
Expected: `201` with an elder `id`. Record it. *(`OPERATOR_API_KEY` is the value you set in `.env.prod`; the management plane uses HTTP Bearer auth — verified in `apps/api/src/usan_api/auth.py` (`require_operator_token` → `HTTPBearer`), so the token goes in `Authorization: Bearer`, not `x-api-key`.)*

- [ ] **Step 2: Outbound — human-answer path (this confirms Gemini billing end-to-end)**

```bash
python3 scripts/place_test_call.py --elder-id <ELDER_ID> \
  --base-url https://api.usanretirement.com --key live-outbound-1 --var greeting=hello
curl -s -H "Authorization: Bearer $OPERATOR_API_KEY" https://api.usanretirement.com/v1/calls/<CALL_ID>
```
Expected: your phone rings; on answer you hear the recording disclosure + greeting and a check-in with a **real LLM turn** (if the agent is silent/apologetic, Gemini billing is the suspect); on hangup the call reaches `completed` with `answered_at`/`ended_at`/`duration_seconds` set and an egress recording in GCS. Record: rang? what you heard, rough first-audio latency, and:
```bash
gcloud storage ls gs://usan-retirement-call-recordings/recordings/   # recording landed?
```

- [ ] **Step 3: Outbound — voicemail path**

Place a call (`--key live-voicemail-1`) and let it reach voicemail.
Expected: status `voicemail_left` (first-3s regex fires, scripted message plays, hangup). **Validate the classifier:** capture the STT transcript from the agent logs and confirm it matched `services/agent/.../voicemail.py`'s patterns/window; adjust if the live greeting shape diverges. Record the observed status.

- [ ] **Step 4: Outbound — no-answer + busy/declined paths**

Place `--key live-noanswer-1` and silence/decline past ring timeout; place `--key live-busy-1` against a busy line if feasible.
Expected: `no_answer` (and `busy` where applicable); the retry orchestrator inserts a follow-up `calls` row (`parent_call_id` set, `attempt=2`) subject to the 09:00–21:00 quiet-hours clamp. **Validate the classifier:** confirm the live LiveKit dial-exception shape maps correctly in `apps/api/.../sip_status.py`; if a call lands in a generic `failed` bucket, capture the real exception and adjust. Record both rows' statuses.

- [ ] **Step 5: Inbound — known elder, then unknown number**

Dial the Telnyx DID from the registered phone, then from an unregistered number.
Expected (known): greeted **by name** within ~2s; a `direction=inbound` row reaches `completed` with a transcript. Expected (unknown): generic greeting; row with `elder_id: null`. Verify:
```bash
curl -s -H "Authorization: Bearer $OPERATOR_API_KEY" https://api.usanretirement.com/v1/calls/<CALL_ID>
```
Record both results.

- [ ] **Step 6: Write the results section into `infra/README.md` and commit**

Append `## Live smoke results (Plan 4c)` documenting the date, VM IP/region, and the observed outcome + latency note for each of Steps 2–5, plus any classifier adjustments made. Then:

```bash
git add infra/README.md
git commit -m "docs(infra): record Plan 4c provisioning + live smoke results"
```

---

## Self-Review

**1. Spec / blocker coverage:**
- Hard blocker "VM never provisioned (no tfstate)" → Tasks 1+3 (apply). ✅
- Hard blocker "Secret `usan-prod-env` has no version" → Task 4 (push version + reset to materialize `.env`); the boot-ordering trap is sequenced explicitly. ✅
- Hard blocker "DNS doesn't resolve" → Task 5 (gated before Task 8). ✅
- Hard blocker "two required tfvars unset (`telnyx_sip_signaling_source_ranges`, dead `image_tag`)" → Task 1 (add the first, delete the second; `terraform plan` in Task 3 asserts the fix). ✅
- Hard blocker "no `v*` tag, deploy gated on tag" → Task 8 (cut `v0.1.0`). ✅
- Soft blocker "`IMAGE_TAG=latest` (moving tag)" → Task 4 Step 1 (set explicit `v0.1.0`). ✅
- "Gemini billing-gated" → Prereq 2 (resolved 2026-06-03) + Task 9 Step 2 (end-to-end confirmation). ✅
- "GHCR images / agent-base:local caveat / GCP APIs" → confirmed NON-blockers (prod overlay pulls GHCR; `iamcredentials` enabled by `storage.tf`); no task needed. ✅
- "Live SIP/voicemail classifiers never validated" → Task 9 Steps 3–4 (validate + adjust). ✅
- **Correctly deferred to Plan 4d** (listed in the Scope note, not silently dropped): DB backups, observability/ops-agent/alerting, log rotation + healthchecks, worker concurrency bound + capacity load-test, the daily-call scheduler, consent/opt-out + distress escalation, vendor BAAs, RetellAI cutover plan. ✅

**2. Placeholder scan:** No "TBD"/"handle errors"/"similar to Task N". Operator-supplied values (`<vm_external_ip>`, `+1YOURPHONE`, `<ELDER_ID>`, `<CALL_ID>`, the GHCR PAT, the SSH key path) are intentional inputs, documented as such — not plan placeholders. Every command has an expected result.

**3. Name/value consistency:** Secret `usan-prod-env`, bucket `usan-retirement-call-recordings`, domains `api.`/`lk.usanretirement.com`, tag `v0.1.0` (Task 4 `IMAGE_TAG` == Task 8 git tag), SA `usan-vm`, VM `usan-vm` / zone `us-east1-b`, and the GitHub secret names (`DEPLOY_HOST`/`DEPLOY_USER`/`DEPLOY_SSH_KEY`/`API_DOMAIN`/`GHCR_PAT`) match `build.yml` and across tasks. The Telnyx CIDR list in `terraform.tfvars` (Task 1 Step 4) equals the example (Step 3) and `variables.tf:52-56`.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-03-plan-4c-provision-first-call.md`. Two execution options:**

**1. Subagent-Driven (recommended for Tasks 1–2 only)** — fresh subagent per task, review between tasks. The Terraform-edit tasks are independently verifiable (`terraform fmt`/`validate`, `grep`) without cloud access.

**2. Inline / interactive (required for Tasks 3–9)** — these are one-time operator actions against live GCP/DNS/Telnyx/your phone and must be run interactively in this session with you (they create real cloud resources, cost money, and place real phone calls). They cannot be delegated to an unattended subagent.

**Which approach?**
