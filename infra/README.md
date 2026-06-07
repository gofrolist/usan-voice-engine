# infra — manual setup

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
4. **First deploy:** set the GitHub Actions secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, and `API_DOMAIN`. Images are pulled from Artifact Registry **keyless** — the VM authenticates via its attached service account (`artifactregistry.reader`) and CI pushes via Workload Identity Federation (Plan 4e E), so **no `GHCR_PAT` is needed**. Push a version tag (`git tag v0.1.0 && git push origin v0.1.0`) — the `deploy` job in `build.yml` waits for the image build, then ships the compose files and brings the stack up. Or deploy manually:
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

## Prerequisites
- `livekit-cli` installed: `brew install livekit-cli` or download from https://github.com/livekit/livekit-cli/releases
- A Telnyx number, SIP trunk, and credentials configured at https://portal.telnyx.com/

## Telnyx side
1. Buy a US phone number (Telnyx portal → Numbers → Buy Numbers).
2. Create a SIP Trunk (Voice → SIP Trunking → Add Trunk):
   - Credential-based auth
   - Note the **trunk's connection URI** (e.g. `sip.telnyx.com`) and the credentials you set
3. Assign the purchased number to the trunk (Numbers → Manage Numbers → Voice Settings → assign to Trunk).
4. Configure inbound SIP signaling URI to point at our livekit-sip:
   - For local dev: not externally reachable; use ngrok or skip until deploy
   - For prod: the VM's public IP on UDP 5060
5. Put the number, SIP username, and SIP password into `infra/.env`.

## LiveKit side

### Inbound trunk + dispatch (auto-applied by the deploy)

The inbound `SIPInboundTrunk` (`usan-telnyx-inbound`) and `SIPDispatchRule`
(`usan-inbound-default` → agent `usan-agent`) live only in LiveKit/redis runtime
state, so a redis/stack wipe drops them. The deploy job runs
**`infra/provision-sip-inbound.sh`** after `compose up` on every release to (re)apply
them idempotently from `infra/.env` (`TELNYX_INBOUND_DID` + LiveKit creds). To apply
by hand on the VM:

```bash
sudo bash /opt/usan/infra/provision-sip-inbound.sh
```

The script matches the DID in both `+E.164` and bare `E.164` forms (Telnyx's "E.164"
inbound format omits the `+`) and allowlists Telnyx's **signaling** ranges
(`192.76.120.0/24`, `64.16.250.0/24`) — see the note in `provision-sip-inbound.sh`.

The **outbound** trunk (`usan-telnyx-outbound`) is auto-provisioned by the API on the
first dial — no manual step.

### Manual `livekit-cli` (only if not using the script)

> **CLI verbs + URL:** the current `livekit/livekit-cli` (the `lk` CLI) uses
> `sip inbound create` / `sip dispatch create` (NOT the older `sip create-trunk` /
> `create-dispatch`). Run from your shell, not inside the compose network, so target
> the host-published port `--url ws://127.0.0.1:7880` (the `.env`
> `LIVEKIT_URL=ws://livekit:7880` is a compose-internal name, unreachable here). No
> `lk` on the VM? Use `docker run --rm -i --network host -e LIVEKIT_URL=ws://127.0.0.1:7880
> -e LIVEKIT_API_KEY -e LIVEKIT_API_SECRET livekit/livekit-cli:latest sip inbound create -`.

```bash
# Inbound trunk — match the DID in BOTH +E.164 and bare-E.164 (Telnyx's "E.164" inbound
# format omits the +), exactly as provision-sip-inbound.sh does. Do NOT just
# `envsubst < livekit-sip-trunk.json`: that template carries only the +E.164 form and
# would miss Telnyx's bare-E.164 To.
printf '{"trunk":{"name":"usan-telnyx-inbound","numbers":["%s","%s"],"allowed_addresses":["192.76.120.0/24","64.16.250.0/24"]}}' \
  "$TELNYX_INBOUND_DID" "${TELNYX_INBOUND_DID#+}" | livekit-cli sip inbound create -
livekit-cli sip dispatch create infra/livekit-sip-dispatch-rule.json
# verify
livekit-cli sip inbound list
livekit-cli sip dispatch list
```

## Inbound flow (Plan 3d)

Inbound is now a personalized check-in. When a call arrives, the dispatch rule
spawns a metadata-less agent job (treated as inbound). The agent reads the SIP
caller-ID (`sip.phoneNumber`), POSTs it to `POST /v1/calls/inbound` (worker-token
authed), and the API looks the caller up by `phone_e164`:

- **Known elder** → an inbound `calls` row (`direction=inbound`, `in_progress`,
  `answered_at` set) is created, `dynamic_vars` (`elder_name` + last check-in) are
  returned, and the agent runs the full tool-driven check-in (wellness + medication
  logging, transcript flush) opening with a greeting by name.
- **Unknown/absent number** → a call row with `elder_id = NULL` is recorded and the
  agent gives the generic greeting only (no per-elder tools).

DNC is **not** checked on inbound (DNC governs outbound dialing only).

### Smoke test

With Telnyx pointing inbound SIP at your livekit-sip endpoint and the dispatch rule
applied (see "LiveKit side" above), and an elder whose `phone_e164` matches the
number you will call from:

```bash
# Register the elder you will call in as (E.164 must match your caller-ID)
curl -s -X POST http://localhost:8000/v1/elders -H 'content-type: application/json' \
  -d '{"name":"Test Elder","phone_e164":"+1YOURPHONE","timezone":"America/New_York"}'
```

1. Dial your Telnyx number from that phone.
2. Within ~2 seconds you should be greeted **by name** and asked how you're feeling.
3. Answer; the agent logs wellness/medications and ends the call.
4. Verify the call + transcript:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f api | grep -i inbound
# find the inbound call_id in the logs, then:
curl -s http://localhost:8000/v1/calls/<CALL_ID>   # direction=inbound, status completed
```

Calling from an unknown number instead yields the generic greeting and a call row
with `elder_id: null`.

## Outbound calling (Plan 2a)

Outbound calls need a LiveKit **SIP outbound trunk** pointing at Telnyx, plus a
caller-ID number. The agent worker is now **named** (`AGENT_NAME=usan-agent`), so
both inbound (dispatch rule) and outbound (explicit dispatch) reference that name.

### 1. Outbound trunk (auto-provisioned)

The API **auto-provisions** the outbound trunk on the first outbound dial: it
reuses a trunk named `usan-telnyx-outbound` if one already exists, otherwise it
creates one from the Telnyx SIP credentials in `infra/.env` (`TELNYX_SIP_USERNAME`,
`TELNYX_SIP_PASSWORD`, `TELNYX_SIP_HOST`) with `TELNYX_CALLER_ID` as the number,
then caches the resolved `ST_...` ID for the process lifetime. **No manual step
and no `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` are required — leave it blank.** Override
the trunk name with `LIVEKIT_OUTBOUND_TRUNK_NAME` if desired.

To **pin** a specific trunk instead (skipping auto-provisioning), create it
manually and set its ID:

```bash
set -a; . infra/.env; set +a
envsubst < infra/livekit-sip-outbound-trunk.json > /tmp/outbound.json
livekit-cli sip create-trunk \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --file /tmp/outbound.json
# Copy the returned ST_... into infra/.env as LIVEKIT_SIP_OUTBOUND_TRUNK_ID, then
# recreate api: docker compose --env-file infra/.env -f infra/docker-compose.yml up -d api
```

### 2. (Re)apply the inbound dispatch rule with the agent name

```bash
envsubst < infra/livekit-sip-dispatch-rule.json > /tmp/rule.json
livekit-cli sip create-dispatch \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --file /tmp/rule.json
```

### 3. Place an outbound call

```bash
# Add an elder
curl -s -X POST http://localhost:8000/v1/elders \
  -H 'content-type: application/json' \
  -d '{"name":"Test Elder","phone_e164":"+1YOURPHONE","timezone":"America/New_York"}'

# Enqueue a call (use the returned elder id)
curl -s -X POST http://localhost:8000/v1/calls \
  -H 'content-type: application/json' \
  -d '{"elder_id":"<ELDER_ID>","idempotency_key":"smoke-1","dynamic_vars":{}}'

# Poll status
curl -s http://localhost:8000/v1/calls/<CALL_ID>
```

Your phone should ring; on answer you hear the greeting. Watch the agent:

```bash
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f agent
```

### Outbound smoke test result

Document the outcome here: network setup (local public IP / VM), what you heard,
and any latency observations. If you don't yet have a public IP for Telnyx to
route media to, note that outbound is deferred to the Plan 4 deploy task.

## Call recording (Plan 4b)

Recordings are written to a GCS bucket by the `usan-egress` container (LiveKit Egress)
and served as short-lived signed URLs by the API.

1. `cd infra/terraform && terraform apply` — provisions the `recordings_bucket`,
   grants the VM service account `roles/storage.objectAdmin` on it and
   `roles/iam.serviceAccountTokenCreator` on itself, and enables
   `iamcredentials.googleapis.com`. Note the `recordings_bucket` output.
2. Set `GCS_BUCKET=<that bucket>` in the `usan-prod-env` Secret Manager secret.
   Optionally set `RECORDING_SIGNED_URL_TTL_S`.
3. Redeploy — the `usan-egress` container ships with the stack and uploads using
   the VM's attached service account (no key files). Leaving `GCS_BUCKET` blank
   disables recording.

Recordings land at `gs://<bucket>/recordings/YYYY-MM-DD/<call_id>.ogg`
(Opus mono); lifecycle moves them to Nearline at 30d and deletes at 1y.
`GET /v1/calls/{id}` returns `presigned_recording_url` (1h TTL) when a recording exists.

## Call lifecycle (Plan 2b-1)

After dispatch, an outbound call now transitions through real states instead of
sitting at `dialing`:

- `dialing` → background SIP dial (`wait_until_answered=true`, `ringing_timeout`):
  - answered → `in_progress` (sets `answered_at`, `sip_call_id`)
  - SIP 486 → `busy`; SIP 408/480/487 → `no_answer`; SIP 603 → `no_answer`; other → `failed` (the room is torn down so the waiting agent exits)
- `in_progress` → LiveKit `room_finished` webhook → `completed` (sets `ended_at`, `duration_seconds`)

LiveKit posts room events to `http://api:8000/webhooks/livekit`, signed with
`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` and verified by the API. The `webhook`
block lives in the `LIVEKIT_CONFIG` env body of the `livekit` service in
`docker-compose.yml` (not a mounted YAML file — livekit-server does not expand
`${...}` in a `--config` file, so the whole config is passed via the env body
where compose substitutes the key). Confirm wiring:

```bash
# place a call, then watch it advance
curl -s -X POST http://localhost:8000/v1/calls -H 'content-type: application/json' \
  -d "{\"elder_id\":\"<ELDER_ID>\",\"idempotency_key\":\"lc-1\",\"dynamic_vars\":{}}"
# poll a few times — status moves dialing -> in_progress/busy/no_answer -> completed
curl -s http://localhost:8000/v1/calls/<CALL_ID>
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f api | grep -i webhook
```

> Retry of `no_answer`/`busy`/`failed`/`voicemail_left` calls and TCPA quiet hours
> are Plan 2b-3; agent-side voicemail detection is Plan 2b-2. In 2b-1 those terminal
> states are reached and recorded but not yet retried.

## Voicemail detection & agent→API auth (Plan 2b-2)

On an answered outbound call the agent listens to the first ~3s of speech. If it
matches a voicemail greeting (e.g. "leave a message", "you've reached", "after the
beep"), the agent cancels the conversation, plays a scripted leave-message, reports
`voicemail_left` to the API, and hangs up (`delete_room`). An unanswered outbound
call ends cleanly via an agent-side answer timeout.

The agent→API report (`POST /v1/calls/{id}/outcome`) is authenticated with a
short-lived HS256 JWT signed with the shared `JWT_SIGNING_KEY`; the API verifies
the signature and that the token's `call_id` matches the path. Set a strong
`JWT_SIGNING_KEY` (`openssl rand -hex 32`) in `infra/.env` — it is required by
both `api` and `agent` at startup.

> Telnyx AMD is intentionally NOT used (our SIP-trunk topology can't invoke it).
> Retry of `voicemail_left` (one attempt after 3h) and TCPA quiet hours are Plan 2b-3.

## Retry orchestrator

The API runs an in-process poller (`retry_orchestrator.run_poller`) that re-dials
calls per the §5.3 policy:

| End state | Retries |
|---|---|
| `no_answer` | +30 min, then +2 h (3 attempts total) |
| `voicemail_left` | +3 h (2 attempts total) |
| `busy` | +5 min (2 attempts total) |
| `failed` (transport) | +1 min (2 attempts total) |

Each retry is a new `calls` row linked to its predecessor by `parent_call_id`
(`attempt = parent.attempt + 1`). A partial UNIQUE index on `parent_call_id`
guarantees at most one retry per attempt. DNC is re-checked at dial time.

**TCPA quiet hours:** retries are never placed before 09:00 or at/after 21:00 in
the elder's local timezone. An invalid elder timezone fails CLOSED (the retry is
not scheduled and an ERROR is logged) rather than risking an out-of-hours call.

**Initial calls** (`POST /v1/calls`) are NOT quiet-hours gated and dial
immediately — the upstream scheduler owns quiet hours for the first attempt
(spec §10 scopes orchestrator enforcement to retries).

**Multiple replicas:** each replica runs its own poller. `FOR UPDATE SKIP LOCKED`
plus the `parent_call_id` unique index make claiming and scheduling safe without
leader election. Set `RETRY_POLLER_ENABLED=false` to confine the poller to a
single replica if preferred.

A stuck-`dialing` reaper re-queues retry rows left in `dialing` by an ungraceful
process death after `RETRY_STUCK_DIALING_S` (must exceed the ring timeout).

## Monitoring (Grafana + Prometheus) — MON-2

Prometheus scrapes `api:8000/metrics` over the bridge (the endpoint is 403'd at the
public edge by Caddy). Grafana is reverse-proxied by Caddy at `grafana.<domain>`,
gated to `GRAFANA_ALLOWED_CIDR` at L7, and requires its own login.

**Ordering matters — `terraform apply` BEFORE Alembic migration 0009.**
`google_sql_user.grafana_ro` (Terraform) and the role's GRANTs (migration `0009`)
both touch the `grafana_ro` role. Apply Terraform first so the role is created with
its password, then run migrations (the migration's `IF NOT EXISTS` guard makes it a
no-op on the already-created role and just applies the GRANTs). If migration `0009`
ran FIRST (e.g. on a dev/staging DB), `terraform apply` will fail with a Cloud SQL
409 (role already exists) — recover by either `DROP ROLE grafana_ro` on the instance
and re-applying, or `ALTER ROLE grafana_ro WITH PASSWORD '<terraform output>'` then
`terraform import google_sql_user.grafana_ro <instance>/grafana_ro`.

One-time setup before the first deploy that includes the monitoring overlay:

1. `terraform apply` — creates the `grafana_ro` Cloud SQL user, the generated
   passwords, the `grafana.<domain>` DNS record, and the `roles/monitoring.viewer`
   binding (read-only; pre-granted for the MON-3 Cloud Monitoring datasource).
2. Fold the new values into the `usan-prod-env` Secret Manager secret (the `.env`):
   - `GRAFANA_DOMAIN=grafana.<domain>`
   - `GRAFANA_ALLOWED_CIDR=<your office/VPN CIDR>` (must be set, or compose aborts)
   - `GF_SECURITY_ADMIN_PASSWORD=$(terraform output -raw grafana_admin_password)`
   - `GF_POSTGRES_RO_PASSWORD=$(terraform output -raw grafana_ro_password)`
   - `GRAFANA_DB_HOST=<cloud sql private ip>:5432` (same host as DATABASE_URL)
   - `GRAFANA_DB_SSLMODE=require`
3. Ensure migration `0009` has been applied to Cloud SQL (the GRANTs for `grafana_ro`)
   via the same path that applies all migrations to prod.
4. Cut a `v*` tag → the deploy workflow ships the overlay + config dirs and brings up
   `prometheus` + `grafana`.

Verify post-deploy:
- `curl -fsS https://api.<domain>/metrics` → **403** (edge-blocked; good).
- From an allowlisted IP, open `https://grafana.<domain>` → Grafana login.
- From a non-allowlisted IP → **403**.
- In Grafana → Connections → Data sources: Prometheus and Postgres both "working".

### Dashboards (MON-3)

The four Grafana dashboards are checked-in JSON under `infra/grafana/dashboards/`
(`latency.json`, `cost.json`, `business.json`, `system.json`), loaded by the
file provider MON-2 installed (folder **USAN**, container path
`/var/lib/grafana/dashboards`, 30 s rescan, `allowUiUpdates=false` so the repo is
the source of truth).

**They ship automatically.** `build.yml` already scp's the whole `infra/grafana`
tree to the VM, so a `v*` tag deploy copies the new JSON; Grafana picks it up
within 30 s. No compose, datasource, or workflow change is needed.

**Datasources they bind to** (provisioned by MON-2, referenced by uid):
- `postgres-ro` — Latency, Cost, Business/Care (read-only `grafana_ro` role).
- `prometheus` — System/RED.

**Host CPU/mem/disk are not here** — no Cloud Monitoring datasource is
provisioned. View host metrics in the GCP Cloud Monitoring console, or wire the
Google Cloud Monitoring datasource + `roles/monitoring.viewer` in a follow-up.

**Verify after deploy** (from an operator IP inside `GRAFANA_ALLOWED_CIDR`):
1. Browse `https://grafana.<domain>/dashboards` → folder **USAN** lists all four.
2. Open **USAN · System (RED)** → "Service up (API)" shows UP and request-rate
   panels populate (Prometheus path healthy).
3. Open **USAN · Latency** → panels render without a datasource error (confirms
   the `grafana_ro` Postgres path + `turn_metrics` access).
4. If a panel shows "datasource not found", the dashboard JSON references a uid
   other than `prometheus` / `postgres-ro` — fix the JSON, not Grafana.

**Edit/add a dashboard:** change the JSON under `infra/grafana/dashboards/`,
keep `id: null` and a unique `uid`/`title`, run `python -m pytest scripts/tests`,
commit, and ship on the next tag. CI's `pytest (scripts)` job validates structure
(datasource uids, gridPos, no PHI columns) on every PR.
