# Surface 2A — Inbound-call-router (synchronous inbound routing egress)

**Date:** 2026-07-09
**Status:** design → build
**Depends on:** Surface 3 external-tools egress (reuses the SSRF-pin + allow-list pattern), the
compat `agent_id` codec (`compat/ids.py`), and `resolve_agent_config`'s `profile_override`
precedence.
**Migration spec:** `~/gofrolist/usan-retirement-backend/VOICE_PROVIDER_MIGRATION_SPEC.md` §3.

---

## 0. What Surface 2A is

When someone dials one of the client's DIDs, **our voice engine must ask the client's backend
who to be** before the agent speaks. We make a synchronous HTTP call to the client's
`inbound-call-router`, and it tells us which agent to run and with what variables. This is the
inbound mirror of the outbound `create-phone-call` override: the *client* owns the routing
decision (their leads/trials/DNC database lives in Supabase, not in our service), so for the
migration we delegate agent selection to them on every inbound call.

**Contract (byte-for-byte, migration spec §3 — "повторяй контракт Retell байт-в-байт"):**

We POST:
```http
POST <COMPAT_INBOUND_ROUTER_URL>[?caller_secret=<secret>]
Content-Type: application/json

{ "event": "call_inbound", "call_inbound": { "from_number": "+1YYY…", "to_number": "+1XXX…" } }
```

They reply (strict shape — the wrapper and field names are load-bearing; the old flat
`{agent_id, retell_llm_dynamic_variables}` shape was silently ignored by Retell and must **not**
be re-emitted):
```json
{ "call_inbound": { "override_agent_id": "agent_<hex>", "dynamic_variables": { "first_name": "John", "trial_status": "active" } } }
```

**Degrade-on-failure is mandatory.** On any non-2xx, missing `call_inbound`, malformed body,
timeout, or an `override_agent_id` we can't resolve to a published agent, we **fall back to the
DID's default inbound agent and let the call through** — an inbound call must always connect.
The router is an enhancement, never a gate.

---

## 1. Where it plugs in

The inbound data-plane already has the exact seam we need:

```
SIP caller → LiveKit dispatch (no metadata) → worker._run_inbound
   participant present → phone = sip.phoneNumber
   → api_client.start_inbound_call(phone, room)         [worker → apps/api]
       → POST /v1/calls/inbound  (register_inbound_call)
           looks up contact, builds vars, creates Call row
       ← {call_id, contact_known, dynamic_vars, resolved_vars, timezone}
   → build_inbound_agent(cfg, …)   (cfg = inbound default, fetched earlier)
```

`register_inbound_call` (`routers/calls.py`) is **the** inbound resolution point in `apps/api`,
and it already owns everything Surface 2A needs: the DB session, the compat `agent_id` codec, the
SSRF guard, and the settings. The router egress goes **there**, not in the worker — identical to
the Surface 3 decision to keep egress + secrets in `apps/api` and off the worker, respecting the
`apps/api` ↔ `services/agent` no-import boundary.

**Config-override propagation.** The override agent's config (prompts/voice/tools) differs from
the inbound default that the worker fetched before `_run_inbound`. Rather than thread a whole
`AgentConfig` back through `InboundCallResponse`, we reuse the existing
`profile_override` → `resolve_agent_config` precedence:

1. `register_inbound_call` decodes `override_agent_id` → profile UUID, validates it's a published
   voice profile, and stores it as `Call.profile_override`.
2. It returns `override_applied: true` in `InboundCallResponse`.
3. The worker, seeing `override_applied`, **re-fetches** config with the inbound `call_id`
   (`fetch_agent_config(direction="inbound", call_id=call_id)`). `runtime.get_agent_config`
   already reads `call.profile_override` for any call_id, so the override profile resolves. One
   extra round-trip, and only when an override actually fired.

This means **no new config-transport plumbing** — just one field on the response and a
conditional re-fetch. When the flag is off, none of it runs and inbound behaves exactly as today.

---

## 2. Changes

### apps/api

**`settings.py`** — three knobs, mirroring Surface 3's `_ENABLED`/`_ALLOWED_HOSTS`/`_CALLER_SECRET`
trio. Because the inbound router is a *single* operator-configured URL (not an arbitrary
client-supplied host like external tools), the URL itself is the allow-list — no separate
`_ALLOWED_HOSTS`:
- `compat_inbound_router_enabled: bool` — `COMPAT_INBOUND_ROUTER_ENABLED`, default `False`.
- `compat_inbound_router_url: str | None` — `COMPAT_INBOUND_ROUTER_URL`, default `None`.
- `compat_inbound_router_caller_secret: str | None` — `COMPAT_INBOUND_ROUTER_CALLER_SECRET`,
  default `None`. Appended as `?caller_secret=` when set (the router has no header slot — spec §3
  reserves this query mechanism; ship the capability now, the client wires `verifyCaller` later).

**`compat/inbound_router.py`** (new) — the egress:
```
async def route_inbound(settings, *, from_number, to_number) -> InboundRouterResult | None
```
- Returns `None` (→ degrade) unless the flag is on AND a URL is set.
- Builds `{"event": "call_inbound", "call_inbound": {"from_number", "to_number"}}`.
- Appends `?caller_secret=` when the secret is set.
- SSRF-pins the URL host (`ssrf_guard.resolve_public_or_raise` + `pin_request`) and streams a
  bounded POST with `follow_redirects=False` and a short timeout (`_INBOUND_ROUTER_TIMEOUT_S =
  5.0` — the caller hears silence during this round-trip, so keep it tight).
- Parses `call_inbound.override_agent_id` (required, non-empty str) + `dynamic_variables` (dict,
  coerced to `{str: str}`). Anything else → `None`.
- Never raises: every failure path (network, non-2xx, bad JSON, missing wrapper) returns `None`.

`InboundRouterResult` = `{override_agent_id: str, dynamic_variables: dict[str, str]}`.

**`schemas/call.py`**:
- `InboundCallRequest.to_number: str | None` (same lenient charset guard as `phone_e164`).
- `InboundCallResponse.override_applied: bool = False` (additive; older worker builds ignore it).

**`routers/calls.py` `register_inbound_call`**:
- After computing `phone`, also normalize `to_number = to_e164(body.to_number)`.
- If the flag is on, `result = await route_inbound(settings, from_number=phone, to_number=to_number)`.
- If `result`: `try: pid = ids.decode_agent_id(result.override_agent_id)` and
  `await agent_profiles_repo.is_live_profile(db, pid, channel="voice")`. On success →
  `profile_override = pid`, `override_applied = True`, and **merge** `result.dynamic_variables`
  over `dynamic_vars` (router vars are the client's authoritative CRM context; they win). On
  `CompatError`/unknown/unpublished → log + degrade (no override).
- Pass `profile_override` to `create_inbound_call`; return `override_applied`.

**`repositories/calls.py` `create_inbound_call`**: add `profile_override: uuid.UUID | None = None`.

### services/agent

**`worker.py`**:
- `_dialed_number(participant)` — reads `sip.trunkPhoneNumber` (livekit-sip populates the dialed
  DID here), fallback `sip.to`. Mirrors `_caller_phone`.
- Pass `to_number` into `start_inbound_call`.
- In `_run_inbound`, broaden the personalized-path condition to
  `info.get("call_id") and (info.get("contact_known") or info.get("override_applied"))`, and when
  `override_applied`, re-fetch `cfg = await fetch_agent_config(settings, direction="inbound",
  call_id=call_id) or cfg` before building the agent. (`fetch_agent_config` never raises — it
  degrades to the inbound default, which is exactly the correct fallback.)

**`api_client.py` `start_inbound_call`**: add `to_number: str | None` param → into the payload.

---

## 3. Semantics locked

- **Degradation = the status quo.** On any router failure the endpoint sets no override and
  applies no router vars; the worker takes today's contact-lookup / greet-only path against the
  inbound default profile. That default profile **is** "the DID's default agent" in our model
  (`is_default_inbound`), satisfying spec §3's fallback requirement.
- **Router vars beat contact vars.** In migration mode our contacts table doesn't know the
  caller, so `dynamic_vars` is `{}` and we simply add the router's. In a hybrid, router
  `dynamic_variables` overwrite same-keyed contact vars — the client's CRM is source of truth.
- **`override_applied` gates the personalized path even for an unknown caller.** A migration
  inbound call has no row in our contacts table (`contact_known=False`) but the router still says
  "run Companion for John". `override_applied` makes the worker run the personalized inbound
  agent (override config + router vars) rather than greet-only.
- **agent_id parity.** `override_agent_id` is decoded with the same `compat/ids.py` codec used by
  `create-phone-call` override and validated with the same `is_live_profile(channel="voice")`
  gate — a chat agent or unpublished/unknown agent degrades rather than 500s. Per migration spec
  §7 the client points `RETELL_COMPANION_AGENT_ID` / `RETELL_SALES_AGENT_ID` at our `agent_<hex>`
  ids, so the returned token decodes natively.
- **Single URL, no allow-list env.** Unlike external tools (arbitrary client URLs), the router is
  one operator-set URL; the SSRF public-IP pin still applies (fail-closed against a misconfigured
  internal URL), but no separate host allow-list is needed.
- **PHI-safe logging.** Router egress logs the masked/absent numbers only — never full E.164, the
  response body, or `dynamic_variables` values.

## 4. Ships inert

Default `COMPAT_INBOUND_ROUTER_ENABLED=false` + no URL ⇒ `route_inbound` returns `None`
immediately, `register_inbound_call` is byte-identical to today, the worker never re-fetches, and
`to_number` is captured-but-unused. No migration (all additive: two nullable schema fields, three
settings, one nullable repo param). Flip the flag + set the URL to activate, exactly like
`COMPAT_EXTERNAL_TOOLS_ENABLED` / `WEBHOOK_DELIVERY_ENABLED`.

## 5. Out of scope (follow-ups)

- Enforcing `?caller_secret=` on our side is client-driven (they haven't wired `verifyCaller` to
  the router yet); we only ship the ability to *send* it.
- DID→default-agent mapping by `to_number` (the `PhoneNumber.inbound_agents` JSONB exists but is
  compat-only today). Surface 2A routes via the client's response, not our DID table; a native
  per-DID default is a separate enhancement.
- Zero-gap audio during the router round-trip (same brief-silence tradeoff already documented in
  `_run_inbound`).
