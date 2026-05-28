# infra — manual setup

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

## Smoke test
With Telnyx pointing inbound SIP at your livekit-sip endpoint and dispatch rules in place:

1. Dial your Telnyx number from a real phone
2. Within ~2 seconds, you should hear: "Hello! This is your daily check-in from USAN. How are you feeling today?"
3. Say something
4. The agent should respond with an acknowledgement
5. Hang up
6. Check logs: `docker compose -f docker-compose.yml logs -f agent`

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
