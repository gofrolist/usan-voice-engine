# Feature Specification: RetellAI-Compatible Public API

**Feature Branch**: `003-retellai-api-parity`

**Created**: 2026-06-20

**Status**: Draft

**Input**: User description: "i want to mimic retellai.com API in my app to make migration seamless https://docs.retellai.com/api-references/overview because i already have CRM system built on top of it"

## Overview

The organization already operates a CRM that integrates with RetellAI's public REST API to
create and track outbound/inbound calls, configure agents, run batch campaigns, and receive
call-event webhooks. This feature exposes a **RetellAI-compatible API surface** on the USAN
Voice Engine so the existing CRM can migrate off RetellAI by repointing its base URL and
credential — ideally with **no changes to its integration code**.

Compatibility is measured against the subset of the RetellAI API the CRM actually uses, which
the stakeholder has confirmed covers four areas: **placing & tracking calls, receiving call
webhooks, managing agent/LLM configuration, and bulk/batch calling**.

> **Note on terminology.** Because this feature's whole purpose is wire-level compatibility,
> the spec necessarily references RetellAI's external contract (endpoint paths, field names,
> object shapes, status codes). Those names ARE the requirement, not implementation detail.
> The spec deliberately avoids prescribing internal technology choices.

## Scope

**In scope (confirmed with stakeholder):**

- Authentication compatible with RetellAI (single static Bearer API key).
- Phone call lifecycle: create, get, list, stop, update.
- Call-event webhooks: `call_started`, `call_ended`, `call_analyzed`.
- Agent configuration: create / get / list / update / delete, versioning & publish.
- Response-engine (Retell LLM) configuration referenced by agents.
- Voice lookup (read-only) and concurrency lookup (read-only).
- Batch outbound calling.
- A contact-resolution shim so number-based call creation needs no pre-existing contact.
- Edge translation of identifiers, timestamps, status values, and error envelopes.

**Out of scope (deferred or deliberately not mimicked):**

- Conversation Flow (visual node-graph builder) and its reusable Components.
- Knowledge Base ingestion/management (RAG).
- Chat Agent family and Chat / SMS-chat sessions (this is a voice-first engine).
- Production Web Call `access_token` issuance for browser/SDK joins.
- Voice add / clone / search (voice-cloning marketplace).
- Batch Test / Test Case / Test Run evaluation suite.
- Phone-number purchase / import / assignment management (single-number engine; numbers
  remain manual infrastructure state).
- MCP tool listing, data-export requests, agent playground, and the custom-LLM realtime
  WebSocket protocol.

Out-of-scope RetellAI endpoints MUST still respond predictably (a clear "not supported"
result), never a silent or misleading success.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Drop-in outbound calling (Priority: P1)

The CRM places an outbound call by calling the RetellAI-shaped "create phone call" endpoint
with a destination number, an agent reference, and dynamic variables, then polls "get call"
and "list calls" to follow the call's progress — all using the same request/response field
names and credential style it uses with RetellAI today.

**Why this priority**: Placing and tracking calls is the core reason the CRM exists. Without
this, no migration is possible. It is also the single largest contract mismatch between the
current engine (contact-first, enqueue-with-idempotency, gated) and RetellAI (number-first,
immediate dial), so getting it right de-risks everything else.

**Independent Test**: Point a RetellAI-style client at the new base URL with an issued API
key, call "create phone call" with only a destination number and agent reference, observe a
real outbound call placed, then retrieve it via "get call" and find it in "list calls" with
RetellAI-compatible fields. Delivers end-to-end value on its own.

**Acceptance Scenarios**:

1. **Given** a valid API key and an agent reference, **When** the CRM creates a call with a
   destination number it has never registered as a contact, **Then** the system resolves/creates
   the contact automatically, places the call, and returns a call object with a RetellAI-shaped
   `call_id`, `call_status`, and number fields.
2. **Given** the CRM omits an idempotency key, **When** it retries the same create-call request,
   **Then** the destination is not dialed twice and the original call record is returned.
3. **Given** a placed call, **When** the CRM requests "get call" and "list calls", **Then** the
   responses use the same field names (transcript, recording URL, analysis, timestamps,
   durations) the CRM reads from RetellAI, with timestamps in milliseconds.
4. **Given** a destination on the do-not-call list or within quiet hours, **When** the CRM
   requests a call, **Then** the system returns an explicit error with a machine-readable
   reason and does **not** place the call.
5. **Given** an ongoing call, **When** the CRM calls "stop call", **Then** the call ends and its
   terminal status/disconnection reason are reflected in subsequent reads.

---

### User Story 2 - Call-event webhooks the CRM already understands (Priority: P1)

As calls progress, the CRM receives webhook callbacks (`call_started`, `call_ended`,
`call_analyzed`) in RetellAI's envelope and signature format, so its existing webhook handlers
update CRM records without modification.

**Why this priority**: The CRM is event-driven; it reconciles call outcomes from webhooks, not
just polling. Webhook event names, payload shape, and signature scheme are the second-biggest
behavioral-parity gap. Without compatible webhooks the CRM is "blind" to call results even if
call creation works.

**Independent Test**: Subscribe a destination, place a call, and confirm the CRM's existing
RetellAI signature-verification accepts the delivery and that event names and payload structure
match what its handlers expect.

**Acceptance Scenarios**:

1. **Given** a configured webhook destination, **When** a call starts, ends, and is analyzed,
   **Then** the CRM receives `call_started`, `call_ended`, and `call_analyzed` events in the
   `{ event, call: { … } }` envelope.
2. **Given** the CRM verifies signatures the same way it does for RetellAI, **When** a webhook
   arrives, **Then** its signature passes that verification.
3. **Given** an allow-listed in-infrastructure destination, **When** `call_analyzed` fires,
   **Then** the payload includes full call fidelity (transcript and analysis).
4. **Given** a webhook destination that is **not** on the allow-list, **When** any event fires,
   **Then** no PHI-bearing payload is delivered to it.
5. **Given** a transient delivery failure, **When** the system retries, **Then** the CRM can
   detect/deduplicate redelivery without double-processing.

---

### User Story 3 - Manage agents and response engines over the API (Priority: P2)

The CRM creates, updates, lists, versions, and publishes agents — and the response-engine
(Retell LLM) configuration they reference — through the API using a Bearer key, instead of
requiring a human to use the admin UI.

**Why this priority**: The stakeholder confirmed the CRM provisions agent configuration
programmatically. Calls can be placed against pre-existing agents (US1) without this, so it is
P2 rather than P1, but full "seamless" migration requires the agent/LLM config plane to be
API-key reachable.

**Independent Test**: Using only an API key, create a response engine, create an agent that
references it, publish a version, list agents, and then place a call against that agent — with
request/response field names matching RetellAI's agent and LLM objects.

**Acceptance Scenarios**:

1. **Given** an API key, **When** the CRM creates a response engine, **Then** it receives an
   `llm_id`-style identifier it can reference from an agent's `response_engine`.
2. **Given** an agent payload in RetellAI's shape (response engine reference, voice, language,
   webhook settings), **When** the CRM creates/updates the agent, **Then** the configuration
   round-trips and is usable for calls.
3. **Given** an agent, **When** the CRM lists versions, creates a version, and publishes it,
   **Then** the published version becomes the live configuration, consistent with the engine's
   existing draft/publish/version model.
4. **Given** the CRM references a voice identifier RetellAI would accept, **When** the voice is
   not hosted here, **Then** the system returns a clear, documented error (not an opaque
   validation failure).
5. **Given** agents created via the admin UI, **When** the CRM lists agents over the API,
   **Then** those agents are also visible (a single agent inventory across both planes).

---

### User Story 4 - Bulk / batch outbound calling (Priority: P2)

The CRM launches a batch of outbound calls in one request — many destination numbers, each
with its own dynamic variables and an optional schedule — using RetellAI's "create batch call"
shape.

**Why this priority**: Batch campaigns are a confirmed CRM use, and the engine already has a
close behavioral equivalent (batch + per-target dynamic variables + scheduling), making this a
high-value, lower-risk slice once US1's number-to-contact shim exists.

**Independent Test**: Submit a batch with several destination numbers and per-target variables;
confirm a RetellAI-shaped batch object is returned and that each target results in a gated,
tracked call reusing the US1 contact-resolution and webhook paths.

**Acceptance Scenarios**:

1. **Given** an API key, **When** the CRM submits a batch with `tasks` of destination numbers
   plus per-target dynamic variables, **Then** a RetellAI-shaped batch object (with a
   batch identifier and scheduling fields) is returned.
2. **Given** batch targets that are new numbers, **When** the batch runs, **Then** contacts are
   resolved/created per target without a pre-existing contact requirement.
3. **Given** a batch with a future trigger time, **When** the trigger time arrives, **Then** the
   batch's calls are placed within the configured window and DNC/quiet-hour gating still applies
   per target.

---

### User Story 5 - Supporting lookups & compatibility fidelity (Priority: P3)

The CRM performs read-only lookups RetellAI offers (list/get voices, get concurrency) and
relies on consistent identifier, timestamp, status, and error formats across every endpoint and
webhook so nothing in its integration "string-matches" incorrectly.

**Why this priority**: These reads and fidelity guarantees smooth the migration and prevent
subtle breakage, but the CRM can function without them if its core call/agent/batch paths work.

**Independent Test**: Call "list voices", "get voice", and "get concurrency" and confirm
RetellAI-shaped responses; verify that the same internal call always presents the same
compatibility identifier across create, get, list, and webhook, and that errors use RetellAI's
envelope.

**Acceptance Scenarios**:

1. **Given** an API key, **When** the CRM lists or gets a voice, **Then** it receives a
   RetellAI-shaped voice object mapped from the curated catalog.
2. **Given** an API key, **When** the CRM gets concurrency, **Then** it receives current and
   limit concurrency in RetellAI's shape.
3. **Given** any in-scope error, **When** it is returned, **Then** it uses RetellAI's
   `{ status, message }` envelope and HTTP status conventions.
4. **Given** an out-of-scope RetellAI endpoint, **When** the CRM calls it, **Then** it receives a
   clear, documented "not supported" response rather than a silent or misleading success.

---

### Edge Cases

- **Number-first vs contact-first**: a destination number maps to an existing contact (match by
  phone and/or external id) vs a brand-new contact (lazy create). External id from the CRM is
  preserved for correlation.
- **Missing idempotency key**: synthesized deterministically so retries never double-dial.
- **DNC / quiet-hours block**: explicit error with machine-readable reason; gating is never
  bypassed (regulatory requirement).
- **Unknown / unhosted voice id**: clear documented error instead of an opaque validation reject.
- **Agent referenced by the CRM was created in the admin UI** (or vice versa): single inventory,
  both reachable.
- **Webhook destination not on the allow-list**: no PHI-bearing payload delivered.
- **Duplicate / retried webhook delivery**: CRM can deduplicate; redelivery is detectable.
- **List pagination**: cursor-based listing with "has more" / total semantics matching RetellAI.
- **Timestamp units**: all compat timestamps in milliseconds, durations in `*_ms`, even though
  the native engine stores them differently.
- **Out-of-scope endpoint called**: predictable "not supported" response, logged, not silent.
- **API key revoked or rotated**: subsequent requests rejected with RetellAI's unauthorized
  semantics.
- **Concurrency limit reached**: surfaced consistently (and via the concurrency lookup).
- **Throughput**: the CRM's single key can carry migrated call volume without being throttled
  below RetellAI-equivalent rates.

## Requirements *(mandatory)*

### Functional Requirements

**Authentication & transport**

- **FR-001**: System MUST accept a single static Bearer API key as the sole credential for all
  in-scope compatibility endpoints, mirroring RetellAI's authentication model, so the CRM
  authenticates without code changes.
- **FR-002**: System MUST expose the compatibility endpoints at a base URL and path layout
  matching RetellAI exactly, including its per-endpoint version prefixes (unversioned
  configuration endpoints, version-2 call endpoints, version-3 list-calls).
- **FR-003**: System MUST be able to issue, list, and revoke compatibility API keys scoped to an
  organization, and MUST reject missing, invalid, or revoked keys using RetellAI's unauthorized
  semantics.
- **FR-004**: The compatibility surface MUST be additive — the existing native API and admin UI
  MUST continue to function unchanged.

**Calls (US1)**

- **FR-010**: System MUST accept "create phone call" requests using RetellAI's request fields
  (destination/origin numbers, agent override reference and version, dynamic variables,
  metadata, custom SIP headers) and place an outbound call.
- **FR-011**: System MUST resolve the destination number to a contact by upserting on phone
  number (and external id when supplied), so no pre-existing contact record is required for call
  or batch creation.
- **FR-012**: When the CRM omits an idempotency key, system MUST synthesize one deterministically
  so retried create-call requests never double-dial.
- **FR-013**: System MUST return a call object whose field names, identifier format, status
  values, timestamps (milliseconds), and durations match RetellAI on create, get, and list.
- **FR-014**: System MUST expose get-call, list-calls (with filter criteria, cursor pagination,
  and "has more"/total), stop-call, and update-call with RetellAI-compatible request/response
  shapes.
- **FR-015**: When a create-call request is blocked by do-not-call or quiet-hours gating, system
  MUST return an explicit error response with a machine-readable reason, and MUST NOT place the
  call or bypass gating.
- **FR-016**: System MUST map its internal call lifecycle onto RetellAI call-status values and
  surface terminal outcomes (e.g., voicemail) in the corresponding RetellAI-shaped fields.

**Webhooks (US2)**

- **FR-020**: System MUST emit call-lifecycle webhooks named `call_started`, `call_ended`, and
  `call_analyzed` using RetellAI's `{ event, call: { … } }` envelope.
- **FR-021**: System MUST sign webhook deliveries such that the CRM's existing RetellAI signature
  verification accepts them.
- **FR-022**: Webhook payloads MUST carry full call fidelity (including transcript and analysis)
  to allow-listed destinations; system MUST restrict delivery to destinations attested as within
  covered infrastructure and MUST NOT deliver PHI-bearing payloads to non-allow-listed
  destinations.
- **FR-023**: System MUST allow the CRM to configure webhook destination(s) and event selection
  in a RetellAI-compatible way (agent-level webhook URL / event settings).
- **FR-024a**: Existing inbound webhook signature verification (Telnyx / LiveKit callbacks into the
  native plane) MUST remain in force and unchanged — this is inherited behavior, **not** a new
  compatibility deliverable.
- **FR-024b**: System MUST guard the compat layer's **outbound** webhook destinations against
  server-side request forgery, in addition to the in-infrastructure allow-list of FR-022. (Inbound
  signature verification of webhooks *from* the CRM is out of scope — the CRM is the receiver, not a
  sender, of compat webhooks.)

**Agent & response-engine configuration (US3)**

- **FR-030**: System MUST expose create / get / list / update / delete agent under API-key auth,
  bridging to the existing agent-profile model and accepting RetellAI agent fields (response
  engine reference, voice, language, webhook settings, tags).
- **FR-031**: System MUST expose a response-engine (Retell LLM) resource via create / get / list
  / update / delete that returns a referenceable engine identifier, so agents whose configuration
  references a response-engine identifier round-trip.
- **FR-032**: System MUST support agent versioning and publish operations compatible with
  RetellAI (list versions, create version, publish version), mapped onto the existing
  draft/publish/version-history model.
- **FR-033**: System MUST accept or translate RetellAI voice identifiers; when a requested voice
  is not hosted, system MUST return a clear, documented error rather than an opaque validation
  failure.
- **FR-034**: System MUST expose list-voices and get-voice returning RetellAI-shaped voice
  objects mapped from the curated voice catalog.

**Batch calling (US4)**

- **FR-040**: System MUST accept "create batch call" with RetellAI fields (origin number, tasks
  of destination numbers plus per-target dynamic variables, name, trigger time, reserved
  concurrency), enqueue the batch, and resolve each destination to a contact per FR-011.
- **FR-041**: System MUST return a RetellAI-shaped batch object (batch identifier and scheduling
  fields), and MAY additionally expose the existing list/get/cancel batch operations.

**Cross-cutting compatibility fidelity (US5)**

- **FR-050**: System MUST translate identifiers between internal and RetellAI-shaped formats
  consistently across every endpoint and webhook, so the same internal entity always presents the
  same compatibility identifier (including voice-identifier aliasing).
- **FR-051**: System MUST emit all compat timestamps as Unix epoch milliseconds and durations as
  millisecond fields.
- **FR-052**: System MUST return errors using RetellAI's `{ status, message }` envelope and
  status-code conventions for in-scope endpoints.
- **FR-053**: For RetellAI endpoints that are out of scope, system MUST return a predictable,
  documented "not supported" response, never a silent or misleading success.
- **FR-054**: System MUST apply rate limiting to the compatibility surface while allowing the
  CRM's key throughput sufficient for migrated call volume.
- **FR-055**: All compatibility endpoints that access protected health information MUST require
  authentication and MUST emit structured audit log entries.
- **FR-060**: System MUST expose a concurrency lookup returning current and limit concurrency in
  RetellAI's shape.

### Key Entities *(include if feature involves data)*

- **Compatibility API Key**: a RetellAI-style static bearer credential scoped to an organization;
  issuable, listable, revocable; the basis of all compat authentication.
- **Compatibility Identifier Mapping**: the stable correspondence between internal identifiers and
  RetellAI-shaped identifiers (calls, agents, response engines, batches, voices), so external ids
  remain consistent and reversible.
- **Agent (compat view)**: the externally visible agent object mapped from the engine's
  agent-profile/version model, including the response-engine reference, voice, language, and
  webhook settings.
- **Response Engine / LLM Configuration (compat resource)**: the externally referenceable
  prompt/model configuration that an agent points to.
- **Call (compat view)**: the externally visible call object (identity, direction, status,
  numbers, agent, timestamps/durations, transcript, recording, analysis, disconnection reason)
  assembled from the engine's call and call-derived records.
- **Batch Call (compat view)**: the externally visible batch object (identity, origin number,
  tasks, scheduling, concurrency) mapped from the engine's batch model.
- **Webhook Subscription & Delivery**: the RetellAI-shaped event subscription (events + allow-
  listed destination) and the signed deliveries sent for call lifecycle events.
- **Voice (compat view)**: the externally visible voice object mapped from the curated catalog.
- **Contact (resolution target)**: the engine's existing contact record, resolved or lazily
  created from a destination number / external id so number-first call creation works.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A CRM currently integrated with RetellAI can place a successful outbound call
  against the new service by changing only its base URL and credential — with no changes to its
  call-creation code.
- **SC-002**: At least 95% of the distinct RetellAI request shapes the CRM uses (path + method +
  field names) are accepted unchanged; each remaining shape is documented with a one-line
  migration note. *(Measurable only once the CRM's captured RetellAI-usage inventory exists — see
  Dependencies; it is the denominator for this percentage.)*
- **SC-003**: For at least 99% of completed calls, the CRM receives `call_started`,
  `call_ended`, and `call_analyzed` webhooks whose signatures pass its existing RetellAI
  verification, within 10 seconds of each underlying event.
- **SC-004**: 100% of calls placed via the compatibility API are retrievable via get-call and
  appear in list-calls using the same field names the CRM reads from RetellAI.
- **SC-005**: 100% of webhook deliveries that carry protected health information go only to
  allow-listed in-infrastructure destinations; zero PHI-bearing deliveries reach a non-covered
  destination.
- **SC-006**: 100% of create-call requests blocked by do-not-call or quiet-hours gating return an
  explicit, documented error — never a silent drop and never an un-gated dial.
- **SC-007**: The existing native API and admin UI exhibit zero functional regressions after the
  compatibility surface ships.
- **SC-008**: End-to-end migration of the CRM requires changes to at most two integration points
  (target: base URL and credential only). *(Validated against the CRM's actual integration once its
  captured RetellAI usage is in hand — see Dependencies.)*
- **SC-009**: Every out-of-scope RetellAI endpoint the CRM might call returns a clear "not
  supported" response (0% silent or misleading successes).

## Assumptions

- **Compatibility target is the CRM's actual usage.** The authoritative definition of "seamless"
  is the set of RetellAI endpoints and fields the CRM actually calls; a captured inventory of
  that usage (from the CRM's code or traffic) is the acceptance oracle. Where the CRM does not
  use a RetellAI capability, exact parity for it is unnecessary.
- **Authentication.** The CRM authenticates with a single static bearer API key exactly as with
  RetellAI; the engine issues one (or more) such keys scoped to the organization.
- **Webhook PHI policy (stakeholder decision).** The CRM runs inside the same covered
  infrastructure, so full-fidelity webhook payloads (including transcript and analysis) are an
  internal data flow, not third-party egress. Delivery is restricted to allow-listed, attested
  in-infrastructure destinations as defense against misconfiguration.
- **Blocked-call behavior (stakeholder decision).** Do-not-call / quiet-hours blocks surface as an
  explicit error response with a machine-readable reason; gating is always enforced.
- **Identifier strategy.** Internal identifiers are retained and translated at the edge via a
  stable, reversible mapping to RetellAI-shaped identifiers (including voice-identifier aliasing),
  rather than re-minting native identifiers — chosen as the least-invasive approach; revisit if
  the two identifier spaces risk drifting.
- **Agent provisioning.** Agents may be created via the admin UI or the compatibility API; both
  appear in a single agent inventory visible to the compatibility API.
- **Additive surface.** The native `/v1` API, admin UI, agent runtime, webhook subsystem, and
  single-organization call/runtime plane remain unchanged; the compatibility surface sits beside
  them.
- **Single-number / single-org runtime.** Phone-number management is out of scope; the engine
  continues to operate its existing number(s). If the CRM probes a number-listing endpoint, a
  read-only stub may be added later.
- **Throughput.** The CRM's migrated call volume fits within the engine's existing capacity; the
  CRM's API key may be granted an elevated or dedicated rate-limit bucket.
- **Regulatory constraints persist.** Idempotent outbound dispatch, do-not-call enforcement,
  authenticated PHI access with audit logging, signature verification on inbound webhooks, and
  rate limiting all remain in force for the compatibility surface.

## Dependencies

- A captured inventory of the CRM's current RetellAI API usage (endpoints + fields actually
  called) to serve as the compatibility acceptance oracle.
- The existing contact, call, batch, and agent-profile/version data model and the operator,
  runtime, and admin planes.
- The existing outbound webhook delivery subsystem and inbound webhook signature verification.
- The curated voice and model catalogs.
