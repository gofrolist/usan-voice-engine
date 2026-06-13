# Phase B Research Brief — Workspace/Org Foundations (Multi-Tenancy)

**Date:** 2026-06-10
**Status:** Pre-spec research brief. The Phase B spec waits for the Phase A stack to merge; this document captures the inventory, the two strategic decisions, and the design proposals the spec will be written against.
**Inputs:** tenancy inventory audit (feat/small-unlocks @ 00c1578), auth/identity surface audit, Postgres multi-tenancy retrofit research (2026), comparative platform research (RetellAI / Twilio / Stripe / Vapi).
**Scale assumption:** single tenant today, dozens of tenants at maturity. Single Postgres, asyncpg + in-process SQLAlchemy pool, no PgBouncer.

Path aliases: `API/` = `apps/api/src/usan_api/`, `MIG/` = `apps/api/migrations/versions/`, `UI/` = `apps/admin-ui/src/`, `AGENT/` = `services/agent/src/usan_agent/`, `INFRA/` = `infra/`.

---

## §1 Tenancy inventory summary

The codebase has **zero org/tenant concept today** (no hits for `org_id|organization|tenant` in API, agent, or migrations). The single-tenant assumption inventory:

| Dimension | Count | Notes |
|---|---|---|
| Existing tables (migrations 0001–0015) | **21** | |
| — need a **direct `org_id` column** | **10** | `elders`, `calls`, `call_batches`, `agent_profiles`, `custom_variables`, `webhook_endpoints`, `admin_audit_log`, `admin_users`, `dnc_list`*, `call_schedules` (* pending the DNC global-vs-per-org decision, §7-Q1) |
| — org **derivable via FK parent** (no column needed) | **11** | transcripts, wellness_logs, medication_logs, turn_metrics, call_metrics, agent_profile_versions, follow_up_flags, callback_requests, sms_messages, call_batch_targets, webhook_deliveries |
| **New tables** required | **4** | `organizations`, `org_memberships`, `api_keys`, `inbound_dids` (DID → org routing) |
| Unique constraints that must become **org-scoped** | **9** | headline: `elders.phone_e164`, `agent_profiles.name`, the two partial default-slot uniques, both `idempotency_key` uniques, `custom_variables.name`, `dnc_list` PK, `admin_users` email PK |
| Unique constraints that **stay** as-is | **6** | call/batch/profile-scoped keys + provider-global `telnyx_message_id` |
| Partial indexes that become org-aware | **4** | `idx_calls_due_retries`, `idx_call_schedules_due`, `idx_call_batches_due`, `idx_call_batches_open` |
| Env singletons that become **per-org data** | **~10** | caller ID, outbound trunk, inbound DID, SMS sender/profile/enable, retention days, autonomous daily cap, dialing pause, operator API key, bootstrap admins |
| Env singletons that **stay global** | **~12** | LiveKit creds, JWT signing key, SIP trunk creds (this phase), agent fleet name, VM concurrency cap, poller intervals, GCS bucket (per-org prefix later), statutory quiet-hour bounds |
| Background pollers iterating globally | **4** | retry (30 s), scheduler (60 s, 6 phases), webhook delivery (10 s), retention (daily) |
| Request-path repository functions needing org predicates | **~25** | full list in Appendix A, group D |
| Admin-UI org-context gaps | **whole surface** | no org in session, nav, routes, query-cache keys, or API client |
| **Critical cross-tenant leak** | **1** | webhook outbox fan-out (`API/repositories/webhook_outbox.py:37-55`) selects ALL enabled subscribed endpoints — once two orgs exist, org A's call events deliver to org B's receiver. Must be fixed in the same increment that introduces a second org-capable table. |

Also noteworthy: IDOR exposure on every UUID-path operator endpoint (any valid elder/call/schedule/batch UUID from another org currently resolves), and the cross-org idempotency-key 409/replay oracle. The full itemized work list is **Appendix A**.

---

## §2 Strategic decisions

### §2(a) App-level `org_id` scoping vs RLS as defense-in-depth

**The question:** is tenant isolation enforced only in application code (repository predicates + a session-level automatic filter), or additionally by Postgres row-level security policies?

**Case for app-level only (PlanetScale position):** zero per-transaction overhead, identical behavior under any pooler, trivially testable with normal pytest fixtures, query plans unchanged, cross-tenant pollers/admin paths are just "don't set the context." RLS shifts security logic into the database where "policy misconfiguration, silent failures, and connection pooling interactions are difficult to debug."

**Case for adding RLS (Crunchy/AWS/healthcare-audit position):** app-level scoping has one documented, fatal failure mode — the one forgotten filter, the raw `text()` query, the copied admin-bypass path. Those bugs are invisible until exploited, and here "exploited" means **cross-tenant PHI disclosure**, a reportable HIPAA breach. RLS with the `NULLIF(current_setting('rls.org_id', TRUE), '')::uuid` form **fails closed**: an unscoped query returns zero rows instead of every org's rows. 2026 healthcare-SaaS audit practice increasingly asks for data-layer isolation proof; RLS is the strongest single artifact.

**Repo-specific factors:**
- This repo's PHI posture explicitly treats fail-closed defense-in-depth as a feature (locked 6-year PHI audit sink, frozen audit message constants, DB-re-check-over-JWT-claims everywhere, last-admin lockout guards). RLS is the same philosophy applied to the data layer.
- The worst RLS pitfalls don't apply here: **no PgBouncer / no transaction-mode pooler** (asyncpg in-process pool), so `SET LOCAL` per transaction in the session factory is safe and cheap; dozens of tenants, small tables, so the per-txn round trip is noise.
- The known RLS foot-guns are all mitigable with known patterns: InitPlan-wrapped `(SELECT current_setting(...))` to avoid per-row evaluation; a separate non-owner `NOBYPASSRLS` app role (migrations keep running as owner); policies managed via `op.execute()` in Alembic; CI connecting as the restricted role asserting cross-org reads return empty and cross-org writes raise.
- The pollers are inherently cross-tenant; the standard answer (claim globally under a policy-exempt path, then `SET LOCAL` per claimed job) composes cleanly with the existing `FOR UPDATE SKIP LOCKED` claims (§6).

**RECOMMENDATION: Both, sequenced — app-level scoping is the primary mechanism and ships first; RLS ships as the final Phase-B-internal hardening increment, and is a hard gate before any second org goes live.**
Concretely: (1) repository-level `org_id` predicates + a `contextvars.ContextVar` set in a FastAPI dependency feeding a `do_orm_execute` + `with_loader_criteria` global filter — correctness lives in code and is what gets unit-tested; (2) RLS policies (`NULLIF(...)` fail-closed form, InitPlan-wrapped), `SET LOCAL rls.org_id` in the session factory, dedicated `NOBYPASSRLS` runtime role, CI suite running as that role. App scoping is the seatbelt you steer with; RLS is the airbag. At this tenant count the airbag costs almost nothing, and it converts "we hope no query forgot the filter" into "the database returns nothing for unscoped access" — exactly the posture this repo already pays for everywhere else.

### §2(b) Elder→Contact: one-shot rename vs shim

**The question:** generalize the eldercare-specific domain language (`elders` table, `elder_id` FKs, `elder_name` builtin, `/v1/elders` routes, UI labels) to "Contact" in one coordinated rename, or alias incrementally.

**Case for one-shot rename:** no permanent dual vocabulary; the longer the shim lives, the more new code is written against the old names.

**Case for shim:** the audit identified six **frozen surfaces where rename is not merely risky but breaking**:
1. DB physical names — `elders` + 9 `elder_id` FK columns + migration-era index/constraint names; physical rename touches every historical migration artifact and the Grafana RO grants/raw SQL.
2. Webhook payload field `elder_id` in `call.started`/`call.completed`/`callback.created` — an **externally signed contract**; consumers verify and parse it.
3. Builtin variable `elder_name` + the 10-name freeze — published `agent_profile_versions` snapshots embed `{{elder_name}}`; renaming silently breaks live prompts in production configs.
4. Legacy single-brace `{elder_name}` slots hard-allow-listed in config validation and default prompts (API + agent mirrors).
5. Agent↔API contract fields (`elder_known`, `resolved_vars` keys) — the agent is a separately deployed service.
6. `end_reason="elder_missing"` — queryable data values in historical rows.

A "one-shot" rename therefore cannot actually be one shot: it would need versioned webhooks, dual-emit payloads, prompt-snapshot migration tooling, and a coordinated API+agent deploy — all to rename things customers never see, while the things customers do see (UI labels, route names) are cheap to rename at any time.

**RECOMMENDATION: Shim — and defer the whole Elder→Contact pass out of Phase B entirely.**
Phase B is org foundations; nothing in it requires the rename. When the pass happens (Phase C or later): map a `Contact` ORM class onto the physical `elders` table; keep `elder_id` in webhook payloads forever (or dual-emit); add `contact_name` as a NEW alias builtin and keep `elder_name` forever; rename only the cheap surfaces (UI labels/dirs, internal identifiers, router prefixes with redirect aliases, the env var name as it moves per-org anyway). Eldercare-specific tools (`log_wellness`, `log_medication`, `get_today_meds`) are handled by **per-org tool-catalog gating**, not renaming — a generic "Contact" org simply doesn't enable them. One Phase B rule to avoid digging the hole deeper: new tables and new API fields introduced in Phase B use org-neutral names where natural, but do not touch existing `elder` names.

---

## §3 Key design proposal — per-org API keys

Replaces the single static `OPERATOR_API_KEY` (one bearer for the entire operator data plane). Follows the house webhook-endpoint-secret precedent (server-generated, returned exactly once, never logged) plus Stripe/Twilio conventions, with one upgrade: **hash at rest** (unlike webhook secrets, an API key only needs verification, never re-reading).

**Format** — typed, grep-able in leaks, prefix-identified:

```
usan_sk_live_<8-char key prefix><43-char urlsafe secret>     (live)
usan_sk_test_<8-char key prefix><43-char secret>             (dev/staging)
```

- Secret material: `secrets.token_urlsafe(32)` (256-bit). The 8-char prefix segment is the public key identifier (stored plaintext, shown in UI, used in logs/audit/ratelimit as `api-key:{prefix}`); prefix + last-4 are all the UI ever displays after creation.

**Storage** — `api_keys` table:

```
id           UUID PK server_default gen_random_uuid()
org_id       UUID NOT NULL FK organizations.id
name         TEXT NOT NULL                 -- human label (Retell pattern)
prefix       TEXT NOT NULL UNIQUE          -- lookup key, non-secret
key_hash     TEXT NOT NULL                 -- sha256 hex of the full presented key
created_by   TEXT                          -- admin email (cf. admin_users.added_by)
created_at   TIMESTAMPTZ NOT NULL
last_used_at TIMESTAMPTZ NULL              -- write throttled to >=60s
revoked_at   TIMESTAMPTZ NULL              -- soft revoke; optional future grace timestamp
```

- sha256 (no KDF) is sufficient for 256-bit random secrets; verify with `hmac.compare_digest(sha256(presented), key_hash)`, preserving the existing constant-time idiom (`API/auth.py:55`).
- Secret appears exactly once in the 201 body; never logged, never recoverable (Stripe "can't reveal later / can't recover").

**Auth integration:**
- New dependency `require_org_key → OrgPrincipal(org_id, key_id, prefix)` (frozen dataclass, `AdminPrincipal` pattern) replaces `require_operator_token` on elders/dnc/calls/schedules/batches/webhook-endpoints routers. Lookup by prefix → hash compare → revocation check. DB-backed, so revocation is live (matching the admin-session DB re-check contract). **No JWT, no scoping claims** — authority is the row.
- Legacy bridge: `OPERATOR_API_KEY` stays honored during migration as a bootstrap key mapped to the default org; removed in a later phase.
- Agent plane untouched: service/worker JWTs stay org-blind; org is a server-side derivation (`token → call row → org`). Putting an org claim in the agent's JWT would create a second source of truth asserted by exactly the component that must not assert tenancy.

**Rotation / revocation:**
- Revoke = set `revoked_at` (soft — preserves audit attribution). Rotate = create-new + revoke-old; no in-place rotate endpoint ("secret appears exactly once" philosophy). Stripe-style grace window (revoke-at-future-timestamp so old+new overlap) is a cheap optional column, worth including in the schema even if the UI ships later.
- Endpoints on the **admin-session plane**: `POST /v1/admin/orgs/{org_id}/api-keys`, `DELETE /v1/admin/api-keys/{id}`, both `require_admin_role(ADMIN)` + `admin_audit.record` (names/prefixes only, never secrets).

**Ratelimit keying (two layers):**
1. Keep the pre-auth per-IP ASGI window exactly as-is — it is the unauthenticated-flood bound, and keying pre-auth on the presented prefix would let an attacker mint unlimited fake prefixes to bypass IP limiting.
2. Add a post-verification per-`key_id` fixed window inside `require_org_key` — cross-org fairness, needed the day two orgs share a NAT egress. Same `limits` library, same in-memory single-process caveat.

**Audit/PHI integration:** `admin_audit_log.actor_email = f"api-key:{prefix}"` finally fixes the lossy `"operator-api-key"` sentinel; bind `org_id` (and key actor) as extra fields on the two frozen `phi_audit` helpers — extra bound fields are explicitly safe, messages never change, zero sink/terraform churn.

---

## §4 Org model shape

**`organizations`** (the hard tenancy boundary; every vendor surveyed converged on "a resource belongs to exactly one org"):

```
id            UUID PK
name          TEXT NOT NULL
slug          TEXT UNIQUE NOT NULL          -- log/URL-friendly identifier
status        org_status NOT NULL DEFAULT 'active'   -- active | suspended | closed (Twilio lifecycle)
-- telephony identity (per-org data; single-org values this phase)
caller_id          TEXT        -- replaces TELNYX_CALLER_ID
sms_from_number    TEXT        -- replaces TELNYX_FROM_NUMBER
sms_enabled        BOOL NOT NULL DEFAULT false
-- policy (per-org; defaults mirror today's env values)
retention_days                  INT  NOT NULL
max_autonomous_calls_per_day    INT  NOT NULL        -- per contact per day
autonomous_dialing_paused       BOOL NOT NULL DEFAULT false
max_concurrent_calls            INT  NULL             -- per-org cap; NULL = no org cap (VM cap still applies)
created_at    TIMESTAMPTZ NOT NULL
```

`status='suspended'` is the reversible per-org kill switch (no dialing, no API-key auth, webhooks paused) — incident-response primitive worth having from day one. Outbound trunk name/ID become per-org provisioning state keyed off `caller_id` (the per-process trunk cache becomes keyed by org).

**`org_memberships`** — ship the real membership table now, not the `admin_users.org_id` nullable-column shortcut:

```
org_id  UUID FK NOT NULL
email   TEXT  FK admin_users.email NOT NULL
role    admin_role NOT NULL          -- existing ADMIN | VIEWER enum
PK (org_id, email)
```

Rationale: the `admin_users` email-PK-with-global-role shape is the thing being replaced; doing it via a nullable column means a second auth migration within months. `admin_users` survives as the identity row (email, created/added_by, bootstrap seeding); role moves to the membership; the last-admin lockout guard becomes last-admin-**per-org**. Platform super-admin = `admin_users.is_platform_admin` boolean (no membership rows needed; sees all orgs) — needed anyway for bootstrap seeding. Admin session JWT stays org-free (authentication only); active org resolved per request from membership + an `X-Org-Id` header (Stripe `Stripe-Account` pattern for our own UI), DB-checked every request like role already is. Roles stay two-level this phase; Retell's PHI-scrubbed read-only "Member" tier is noted as the future third role.

**Stays global this phase (deliberately NOT org-scoped):**
- **Statutory TCPA quiet-hour bounds** (`API/quiet_hours.py`) — law, not preference; per-profile narrowing already exists inside the statutory window.
- **DNC list** (proposed default; §7-Q1) — Twilio's lesson: consent/opt-out state must live *above* the tenant or number moves orphan it. A phone that said "stop calling me" should be honored platform-wide.
- **Infra singletons:** LiveKit server creds, `JWT_SIGNING_KEY` (single trusted agent fleet → single key; per-org worker tokens only if orgs ever get separate fleets), Telnyx SIP trunk credentials (shared connection this phase), `AGENT_NAME`, GCS bucket (per-org object prefix, not per-org bucket), VM-level `MAX_CONCURRENT_CALLS` + `RESERVED_CONCURRENCY` (physical capacity; per-org caps layer on top), poller intervals/batch sizes, Caddy/ADMIN_ALLOWED_CIDR, `GOOGLE_OAUTH_HD` (single Workspace domain assumption holds this phase; §7-Q4), Grafana (fleet-level dashboards; per-org views later via the same `org_id` columns).
- **Platform billing/cost metering** — Twilio parent-balance model fits a self-hosted single-operator platform; per-org usage *attribution* falls out of `calls.org_id` joins in the existing cost SQL when needed.

---

## §5 Migration sequencing outline (Phase-B-internal increments)

Everything ships **inert**: one seeded default org, all existing rows mapped to it, behavior byte-identical until a second org is created. Each increment is independently shippable and follows the squash-merged-PR-per-plan workflow.

- **B1 — Org spine.** `organizations` + seeded default org; `org_memberships` backfilled from `admin_users` rows (role copied); `is_platform_admin` for bootstrap emails; last-admin guard becomes per-org. `/v1/auth/me` returns memberships (UI ignores it for now). *Ships inert: one org, same admins, same roles.*
- **B2 — Columns + constraints.** `org_id` on the 10 direct tables via the **PG11 fast-default path** (`ADD COLUMN ... NOT NULL DEFAULT '<default-org-uuid>'` — metadata-only, no rewrite, no backfill), then **`DROP DEFAULT` in the same increment once all write paths stamp `org_id` explicitly** — the lingering default is the hygiene trap that masks scoping bugs. Composite uniques rewritten (`(org_id, phone_e164)`, `(org_id, name)`, `(org_id, idempotency_key)` ×2, `(org_id, external_id)`, per-org default-slot partial uniques); tenant-path indexes gain a leading `org_id`; the four global poller partial indexes deliberately do NOT (cross-tenant scans, low org cardinality). **The webhook fan-out org filter lands here** — the moment `webhook_endpoints.org_id` exists, `enqueue_event` filters on it. DNC advisory-lock keyspace updated only if Q1 resolves per-org. *Ships inert: fast default + immediate explicit stamping.*
- **B3 — Auth plane.** `api_keys` table + `require_org_key`/`OrgPrincipal` per §3; operator routers swapped; `OPERATOR_API_KEY` mapped to the default org as legacy bootstrap key; per-key post-auth ratelimit window; `admin_audit_log.org_id` + `api-key:{prefix}` attribution + `org_id` bound on `phi_audit` helpers. *Ships inert: legacy key keeps working, now attributed.*
- **B4 — App-level scoping.** ContextVar + `do_orm_execute`/`with_loader_criteria` automatic filter; explicit org predicates in the ~25 request-path repository functions (Appendix A group D); IDOR ownership checks on UUID-path lookups; idempotency lookups become `(org_id, key)`. Operator routers derive org from `OrgPrincipal`; admin routers from the resolved active org. *Ships inert: every predicate evaluates to the default org.*
- **B5 — Per-org config + pollers + inbound routing.** Pollers and orchestrators read per-org policy (pause, daily cap, retention, concurrency cap) from `organizations` instead of env (§6); retention purge iterates orgs; `inbound_dids(did → org_id)` table seeded with the single current DID, dial-context plumbed through dispatch metadata so `register_inbound_call` resolves org from DID and the elder-by-phone lookup is org-scoped. Env vars retired from settings or kept as default-org seed values. *Ships inert: one DID row, config values identical to env.*
- **B6 — RLS hardening (gate for second org).** Policies via Alembic `op.execute()` on all org-bearing tables (`NULLIF` fail-closed form, InitPlan-wrapped); dedicated non-owner `NOBYPASSRLS` runtime role (migrations stay owner); `SET LOCAL rls.org_id` in the session factory; poller claim paths run under the cross-tenant-permitted policy then set per-job context; CI job connecting as the restricted role asserting cross-org SELECT → empty and cross-org INSERT → policy violation. *Ships inert: context always = default org.*

**Explicitly deferred out of Phase B:** Elder→Contact pass (§2b); admin-UI org switcher + per-org routes/cache keys (a thin Phase C — the API contract for it, memberships in `/v1/auth/me` + `X-Org-Id`, ships in B1/B4); per-org Grafana views; per-org webhook circuit-breaker/enable flags; per-org Telnyx connections/SIP creds; per-org worker JWTs; fairness scheduling beyond caps (§6); org lifecycle hard-delete tooling.

---

## §6 Poller org-fanning approach

**Keep global claims; denormalize `org_id`; apply per-org policy as claim filters; defer fairness machinery.** This is the dominant pattern at dozens of tenants — per-org queue partitioning (Hatchet/Inngest-style) is unwarranted below hundreds of active tenants.

- **Claim shape unchanged:** single `FOR UPDATE SKIP LOCKED` global claim per poller, now with `org_id` available directly on `calls`, `call_schedules`, `call_batches` (no join — this is why those three get denormalized columns despite FK paths) and via the `webhook_endpoints` join the outbox claim already performs.
- **Per-org policy filters at claim/materialization time:** `claim_due_retries` and `materialize_call` exclude orgs with `autonomous_dialing_paused` or `status='suspended'`; the daily cap reads the org's `max_autonomous_calls_per_day`; the DNC gate stays global (or gains org scope per Q1).
- **Per-org concurrency caps early** — telephony capacity is the real shared resource. `count_in_flight` gains a `GROUP BY org_id` variant; the dial-slot budget becomes `min(global free slots, org cap − org in-flight)`. Reject-vs-queue semantics per §7-Q8 (recommend Vapi-style queue: the call stays QUEUED and is picked up next cycle). `IN_FLIGHT_CALLS`/`DIAL_SLOTS_FREE` gauges stay fleet-level; add an org-labeled variant only if org count stays bounded.
- **Known starvation point, deliberate deferral:** batch target claiming orders by `(batch_id, target_index)` globally — one org's giant batch starves others. The caps above bound the damage (a starved org still gets its concurrency slots). When real noisy-neighbor pain appears, the escalation path is per-org round-robin via `DISTINCT ON (org_id)` — O(orgs), fine at dozens — not write-time block-sequencing.
- **Retention poller** is the one that genuinely fans out: per-org `retention_days` forces an iterate-orgs loop (daily, dozens of orgs — trivial).
- **RLS interaction (B6):** pollers claim globally under a cross-tenant-permitted path, then `SET LOCAL rls.org_id` per claimed job before touching tenant rows — the standard claim-globally-then-scope-per-job hybrid. Housekeeping (sweep/expire/prune, stuck-dialing reclaim) stays global.

---

## §7 Open questions for the user

1. **DNC scope (blocks B2 schema):** keep the DNC list global phone-keyed (recommended — TCPA opt-out is consent state that should live above the org; Twilio's number-move lesson) or per-org `(org_id, phone)`? A middle option: global statutory DNC + optional per-org suppression overlay.
2. **Inbound DIDs:** does each org get its own Telnyx DID (and SMS number)? This is what makes `(org_id, phone_e164)` elder uniqueness resolvable on inbound — without per-org DIDs, an inbound caller whose number exists in two orgs is ambiguous.
3. **Telnyx topology:** shared SIP connection + per-org caller IDs/DIDs (assumed this phase), or separate Telnyx connections per org (separate SIP creds, separate messaging profiles)? Affects whether `TELNYX_SIP_USERNAME/PASSWORD` ever moves into `organizations`.
4. **Admin identity reach:** will all org operators be on the single Google Workspace domain (`GOOGLE_OAUTH_HD`) and inside one `ADMIN_ALLOWED_CIDR`, or do external-org operators need access (per-org `hd` allow-list / CIDR widening / SSO-only gate)?
5. **Second-org timeline + RLS gate:** confirm that B6 (RLS) is a hard prerequisite for onboarding org #2, and roughly when org #2 is expected — this sets Phase B's urgency and whether B1–B5 can ship across several releases.
6. **Per-org concurrency semantics:** when an org hits its cap, queue (call stays QUEUED, picked up next cycle — recommended, Vapi model) or reject at enqueue with an explicit error?
7. **Elder→Contact deferral:** confirm the shim-first, post-Phase-B plan (§2b), and whether any near-term org actually needs non-eldercare vocabulary or just tool-catalog gating.
8. **Webhook policy per org:** should `WEBHOOK_DELIVERY_ENABLED` / circuit-breaker thresholds become per-org in Phase B, or is the per-endpoint circuit breaker sufficient until a real need appears?
9. **Daily-call batch trigger ownership:** the batch trigger driving daily wellness calls lives outside this repo (`enqueue_call` is external-only). Who owns org-fanning that external trigger once orgs exist — does it become an in-repo per-org scheduler in Phase B or stay external with per-org API keys?
10. **Usage metering:** is per-org cost attribution (Grafana per-org cost panels via `calls.org_id`) wanted in Phase B, or deferred until there is a billing conversation?

---

## Appendix A — Full tenancy work list

Legend: **B#** = Phase B increment per §5. `*` = pending §7 decision.

### A. Tables — direct `org_id` column (B2)

| # | Table | Driver | Where |
|---|---|---|---|
| 1 | `elders` | org-owning root entity | `API/db/models.py:33-60`, `MIG/0001:24-36` |
| 2 | `calls` | `elder_id` nullable + SET NULL (inbound unknown-caller, post-delete rows have no org path); hot poller predicates read `calls` join-free | `models.py:73-132`, `MIG/0001:61-84` |
| 3 | `call_batches` | no FK to any org-owning parent at all | `models.py:480-514`, `MIG/0012:57-100` |
| 4 | `agent_profiles` | per-org config + default slots | `models.py:253-291`, `MIG/0010:29-47` |
| 5 | `custom_variables` | per-org catalog; per-org name uniqueness | `models.py:624-652`, `MIG/0015:26-37` |
| 6 | `webhook_endpoints` | fan-out leak fix; per-org 10-endpoint cap | `models.py:559-588`, `MIG/0014:27-58` |
| 7 | `admin_audit_log` | entity refs are free strings; org underivable | `models.py:334-358`, `MIG/0010:76-90` |
| 8 | `admin_users` | email PK → identity row; role → `org_memberships` (B1) | `models.py:314-331`, `MIG/0010:64-74` |
| 9 | `dnc_list` * | per-org only if Q1 says so; else stays global | `models.py:63-70`, `MIG/0001:40-46` |
| 10 | `call_schedules` | denormalized for join-free due-claim + per-org pause/caps | `models.py:435-477`, `MIG/0012:26-53` |

### B. Tables — org via FK parent (no column; verify path in tests)

| # | Table | Org path | Note |
|---|---|---|---|
| 11 | `transcripts` | `call_id → calls` | per-org retention needs the join (B5) |
| 12 | `wellness_logs` | `call_id`/`elder_id`, both NOT NULL | |
| 13 | `medication_logs` | same dual path | |
| 14 | `turn_metrics` | `call_id → calls` | |
| 15 | `call_metrics` | `call_id` is PK | Grafana cost SQL already joins |
| 16 | `agent_profile_versions` | `profile_id → agent_profiles` | |
| 17 | `follow_up_flags` | `call_id → calls` (NOT NULL) | denormalize later if queue lists get hot |
| 18 | `callback_requests` | `call_id → calls` (NOT NULL) | |
| 19 | `sms_messages` | `call_id → calls` (NOT NULL) | |
| 20 | `call_batch_targets` | `batch_id → call_batches` (`elder_id` is SET NULL — not a safe path) | |
| 21 | `webhook_deliveries` | `endpoint_id → webhook_endpoints` | claim SQL already joins endpoints |

New tables: `organizations` (B1), `org_memberships` (B1), `api_keys` (B3), `inbound_dids` (B5).

### C. Unique constraints / indexes (B2)

| # | Constraint | Action |
|---|---|---|
| 1 | `elders.phone_e164` UNIQUE | → `(org_id, phone_e164)` — inbound lookup key; requires DID→org context (B5) |
| 2 | `elders.external_id` UNIQUE | → `(org_id, external_id)` |
| 3 | `dnc_list.phone_e164` PK * | → `(org_id, phone)` or stays global per Q1; advisory-lock keyspace `hashtext(:phone)` follows |
| 4 | `calls.idempotency_key` UNIQUE | → `(org_id, key)` — closes cross-org 409/replay oracle |
| 5 | `call_batches.idempotency_key` UNIQUE | → `(org_id, key)` |
| 6 | `agent_profiles.name` UNIQUE | → `(org_id, name)` |
| 7 | `uq_agent_profiles_default_outbound`/`_inbound` partials | → `UNIQUE(org_id) WHERE is_default_*`; update `set_default` clear-then-set + `get_default_profile` |
| 8 | `custom_variables.name` UNIQUE | → `(org_id, name)`; builtin-collision check stays global |
| 9 | `admin_users.email` PK | → `org_memberships(org_id, email)` PK; last-admin guard per-org (B1) |
| 10–15 | `call_schedules.elder_id`, `sms.telnyx_message_id`, `uq_calls_parent_call_id`, `uq_turn_metrics_call_turn`, `uq_agent_profile_versions_*`, `uq_call_batch_targets_idx` | **stay** |
| — | `idx_calls_due_retries`, `idx_call_schedules_due`, `idx_call_batches_due`, `idx_call_batches_open` | keep org-free leading edge (global scans); add `org_id` as included column for cap filters |
| — | tenant-path indexes (rosters, lists) | rewrite `(org_id, <existing key>)` leading |

### D. Repositories / queries needing org predicates (B4 unless noted)

| Area | Functions |
|---|---|
| Pollers (B5/B6) | `claim_due_retries`, `count_in_flight` (+`GROUP BY org_id`), `count_queued_due`, `reclaim_stuck_dialing`, `reconcile_missing_recordings`; `claim_due_schedules`, `trigger_due_batches`, `claim_next_pending_target`, `open_batches`, `complete_drained_batches`, `count_autonomous_roots` (cap value per-org), `materialize_call` gates; webhook `claim_due` + **`enqueue_event` fan-out (B2 — CRITICAL)**; retention purge per-org loop |
| Elders | `get_elder_by_phone` (hot, inbound, org-scoped via DID), `list_with_profile`, `get_elder`/`update_elder` ownership (IDOR) |
| Calls | `admin_calls.list_calls`, `get_by_idempotency_key` → `(org, key)` |
| Schedules/batches | `list_schedules`, `get_by_elder`, `list_batches`, `get_by_idempotency_key` |
| Queues | `follow_up_flags.list_flags`/`count_by_status`/`count_open_urgent`; `callback_requests.list_*`/`count_*`; `sms_messages.list_messages`; `admin_tools` routers ×6 |
| Profiles/vars | `list_profiles`, `get_default_profile`, `resolve_agent_config`, `resolve_call_policy` (org rides call/elder), `custom_variables.list/names/phi_names` |
| Admin | `admin_audit.list_recent`, `admin_users.*` (per-org), `webhook_endpoints.list/count` (per-org cap) |
| Infra | Grafana dashboard raw SQL (deferred), rate limiting per-key window (B3) |

### E. Env singletons → per-org data (B5 unless noted)

| Setting | Destination |
|---|---|
| `TELNYX_CALLER_ID`, `LIVEKIT_OUTBOUND_TRUNK_NAME`/`_TRUNK_ID` (+ per-org trunk cache) | `organizations.caller_id` + per-org trunk provisioning |
| `TELNYX_INBOUND_DID` (infra-only today) | `inbound_dids` table + dispatch metadata plumbing |
| `TELNYX_FROM_NUMBER`, `TELNYX_MESSAGING_PROFILE_ID`, `TELNYX_MESSAGING_ENABLED` | `organizations.sms_*` |
| `MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY`, `AUTONOMOUS_DIALING_PAUSED`, `PHI_RETENTION_DAYS` | `organizations` policy columns (global kill switch retained) |
| `OPERATOR_API_KEY` | `api_keys` (B3; legacy bridge) |
| `ADMIN_BOOTSTRAP_EMAILS` | per-org seeding / platform-admin (B1) |
| `GOOGLE_OAUTH_HD`, `ADMIN_ALLOWED_CIDR` * | stay global this phase (Q4) |
| `TELNYX_SIP_USERNAME/PASSWORD`, `MAX_CONCURRENT_CALLS`, `RESERVED_CONCURRENCY`, `SCHEDULER_*`, `RETRY_*`, `WEBHOOK_DELIVERY_*` intervals, `JWT_SIGNING_KEY`, `AGENT_NAME`, `GCS_BUCKET`, quiet-hour statutory bounds | stay global (per-org concurrency cap layers on top; GCS per-org prefix later) |

### F. Admin-UI gaps (deferred to Phase C; API contract ships in Phase B)

`NavSidebar` (no switcher, hardcoded brand), `lib/api.ts` (no org header), `queryClient` (no org dimension in cache keys), `useSession`/`RequireAuth` (global role), `AdminUsersPage` (global allow-list), `DefaultsPage` (two-slot whole-system assumption), all roster/list pages (elders, calls, queues, audit, custom variables, profiles), `routes.tsx` (no org segment), `variableCatalog.ts` (single catalog fetch).

### G. Elder→Contact shim inventory (post-Phase-B)

Shim-mandatory (frozen): DB physical names (`elders` + 9 `elder_id` FKs), webhook payload `elder_id`, builtin `elder_name` + 10-name freeze, legacy `{elder_name}` slots, agent↔API fields (`elder_known`, `resolved_vars`), `end_reason="elder_missing"`. Rename-cheap (any time): UI labels/dirs/routes, internal identifiers (~50 API + ~10 agent files), router prefixes with redirect aliases, env var name. Eldercare tools (`log_wellness`/`log_medication`/`get_today_meds`) → per-org tool-catalog gating, not rename.
