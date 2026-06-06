#!/usr/bin/env bash
# Idempotently (re)provision the LiveKit inbound SIP trunk + dispatch rule.
#
# WHY THIS EXISTS: unlike the OUTBOUND trunk (auto-provisioned by the API on first
# dial), the inbound SIPInboundTrunk + SIPDispatchRule live ONLY in LiveKit/redis
# runtime state — nothing in the repo applied them, so a redis/stack wipe silently
# dropped inbound routing (and a fresh deploy never restored it). The deploy job
# (.github/workflows/build.yml) runs this after `compose up` so the inbound route is
# reapplied on every release, surviving wipes. Safe to run by hand too.
#
# Telnyx side (operator, one-time, in the portal) is still required: the inbound DID
# assigned to an IP SIP Connection whose routing points at this VM's IP:5060/UDP, and
# the GCP firewall (terraform usan-allow-sip) allowing Telnyx's SIGNALING ranges.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/usan/infra/.env}"
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

: "${LIVEKIT_API_KEY:?LIVEKIT_API_KEY missing}"
: "${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET missing}"
: "${TELNYX_INBOUND_DID:?TELNYX_INBOUND_DID missing}"

# livekit + livekit-sip are host-networked in prod, so reach the SFU over loopback.
LK_URL="${LK_PROVISION_URL:-ws://127.0.0.1:7880}"
AGENT="${AGENT_NAME:-usan-agent}"
# Pinned (not :latest) so a future CLI release can't change the `sip inbound create`
# interface and break provisioning at deploy time. Override via LIVEKIT_CLI_IMAGE.
CLI_IMAGE="${LIVEKIT_CLI_IMAGE:-livekit/livekit-cli:v2.16.4}"
# Telnyx US SIP SIGNALING source ranges (where inbound INVITEs originate). MUST match
# the firewall's telnyx_sip_signaling_source_ranges. These are NOT the media/RTP CIDRs
# (a call's INVITE comes from 192.76.120.10 / 64.16.250.10, not the media subnets).
SIGNALING_RANGES="${TELNYX_SIP_SIGNALING_RANGES:-192.76.120.0/24,64.16.250.0/24}"

# Telnyx's "E.164" inbound destination format omits the leading +, so match the DID in
# BOTH forms (+19494090011 and 19494090011).
did_plus="$TELNYX_INBOUND_DID"
[[ "$did_plus" == +* ]] || did_plus="+$did_plus"
did_bare="${did_plus#+}"

lk() {
  docker run --rm -i --network host \
    -e LIVEKIT_URL="$LK_URL" -e LIVEKIT_API_KEY -e LIVEKIT_API_SECRET \
    "$CLI_IMAGE" "$@"
}

echo "[provision-sip-inbound] waiting for LiveKit at $LK_URL ..."
for _ in $(seq 1 30); do
  lk sip inbound list >/dev/null 2>&1 && break
  sleep 2
done

# Build JSON array literals.
numbers="\"$did_plus\",\"$did_bare\""
addrs=""
for cidr in ${SIGNALING_RANGES//,/ }; do addrs+="\"$cidr\","; done
addrs="${addrs%,}"

# Idempotent: delete any existing objects with our names, then create fresh. (Deleting
# stale dispatch rules also guards against duplicates spawning multiple agent jobs per
# call.) The brief delete->create gap is sub-second and deploys are infrequent.
echo "[provision-sip-inbound] clearing existing usan-telnyx-inbound / usan-inbound-default ..."
for id in $(lk sip inbound list 2>/dev/null | grep usan-telnyx-inbound | grep -oE 'ST_[A-Za-z0-9]+'); do
  lk sip inbound delete "$id" >/dev/null 2>&1 || true
done
for id in $(lk sip dispatch list 2>/dev/null | grep usan-inbound-default | grep -oE 'SDR_[A-Za-z0-9]+'); do
  lk sip dispatch delete "$id" >/dev/null 2>&1 || true
done

# The create calls have NO `|| true`: under `set -e` a failure here aborts non-zero
# and FAILS THE DEPLOY ON PURPOSE — a broken inbound route should be loud, not silent
# (the rest of the stack is already up from `compose up`).
echo "[provision-sip-inbound] creating inbound trunk for $did_plus (allow: $SIGNALING_RANGES) ..."
printf '{"trunk":{"name":"usan-telnyx-inbound","numbers":[%s],"allowed_addresses":[%s]}}' \
  "$numbers" "$addrs" | lk sip inbound create -

echo "[provision-sip-inbound] creating dispatch rule -> agent $AGENT ..."
printf '{"dispatch_rule":{"name":"usan-inbound-default","rule":{"dispatchRuleIndividual":{"roomPrefix":"usan-inbound-"}},"room_config":{"agents":[{"agent_name":"%s"}]}}}' \
  "$AGENT" | lk sip dispatch create -

echo "[provision-sip-inbound] done."
