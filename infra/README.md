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
