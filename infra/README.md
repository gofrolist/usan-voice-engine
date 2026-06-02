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
4. **First deploy:** set the GitHub Actions secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `API_DOMAIN`, and `GHCR_PAT` (a token with `read:packages`, used by the VM to pull private images from GHCR). Push a version tag (`git tag v0.1.0 && git push origin v0.1.0`) — the `deploy` job in `build.yml` waits for the image build, then ships the compose files and brings the stack up. Or deploy manually:
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
With the stack running and `infra/.env` loaded:

```bash
# Substitute env vars into the JSON files
envsubst < infra/livekit-sip-trunk.json > /tmp/trunk.json
envsubst < infra/livekit-sip-dispatch-rule.json > /tmp/rule.json

# Apply
livekit-cli sip create-trunk \
  --url ${LIVEKIT_URL} \
  --api-key ${LIVEKIT_API_KEY} \
  --api-secret ${LIVEKIT_API_SECRET} \
  --file /tmp/trunk.json

livekit-cli sip create-dispatch \
  --url ${LIVEKIT_URL} \
  --api-key ${LIVEKIT_API_KEY} \
  --api-secret ${LIVEKIT_API_SECRET} \
  --file /tmp/rule.json
```

Verify:

```bash
livekit-cli sip list-trunk
livekit-cli sip list-dispatch
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

### 1. Create the outbound trunk

With the stack running and `infra/.env` populated (Telnyx SIP credentials + `TELNYX_CALLER_ID`):

```bash
set -a; . infra/.env; set +a
envsubst < infra/livekit-sip-outbound-trunk.json > /tmp/outbound.json

livekit-cli sip create-trunk \
  --url "$LIVEKIT_URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --file /tmp/outbound.json
```

Copy the returned trunk ID (`ST_...`) into `infra/.env` as `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`,
then recreate the `api` container so it picks up the new env:

```bash
make up   # or: docker compose --env-file infra/.env -f infra/docker-compose.yml up -d api
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

Recordings are written to a GCS bucket by the `livekit-egress` container and served
as short-lived signed URLs by the API.

1. `cd infra/terraform && terraform apply` — provisions the `recordings_bucket`,
   grants the VM service account `roles/storage.objectAdmin` on it and
   `roles/iam.serviceAccountTokenCreator` on itself, and enables
   `iamcredentials.googleapis.com`. Note the `recordings_bucket` output.
2. Set `GCS_BUCKET=<that bucket>` in the `usan-prod-env` Secret Manager secret.
   Optionally set `RECORDING_SIGNED_URL_TTL_S`.
3. Redeploy — the `livekit-egress` container ships with the stack and uploads using
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
