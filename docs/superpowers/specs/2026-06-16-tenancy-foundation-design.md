# P1 — Multi-Tenant Foundation: organizations + Postgres RLS (design)

**Date:** 2026-06-16
**Status:** Approved (brainstorming) — pending implementation plan
**Branch:** `feat/tenancy-foundation`, off `origin/main`
**Part of:** the "multi-tenant client-facing platform" effort. This is **Phase 1 (P1)** of a 6-phase decomposition; the later phases each get their own spec → plan → implementation cycle.

---

## 0. Why this exists (the motivating decision)

USAN wants `admin.usanretirement.com` to become a **publicly accessible, multi-tenant SaaS** where each customer organization manages only its own data. Today the system is **single-tenant**: there is no `organization`/`tenant` concept anywhere in the schema, and roles are a flat global `ADMIN`/`VIEWER`. Every authenticated user can read every patient's PHI and manage platform admin users. Pointing customers at the current console as-is would expose every client's PHI to every other client — a HIPAA breach, not a configuration gap.

P1 builds the **bedrock that makes "client A can never see client B's data" a database-enforced invariant** — before any second tenant, any client login (P2), or any client app (P4) exists.

## 1. Goals

- Introduce a first-class `organizations` (tenant) entity.
- Tag every PHI-bearing and per-customer-config row with `organization_id`.
- Enforce tenant isolation with **Postgres Row-Level Security (RLS)** — a hard, fail-closed backstop that holds even when application code forgets a filter.
- Provide a **per-transaction tenant-context** mechanism (`SET LOCAL app.current_org`) that the request layer and background workers set.
- Migrate all existing production data into a single default organization, with **zero behavior change** to the running system.
- Ship an **isolation test suite** that proves cross-tenant access is impossible — including with the app-layer filter removed, so the test exercises RLS itself.

## 2. Non-goals (deferred to later phases — do NOT build here)

- **P2** — per-user org binding, org-scoped RBAC, login resolving a user's org, the audited act-as/impersonation wiring. P1 builds the RLS *mechanism* act-as needs, but leaves `admin_users.organization_id` and the identity flow to P2.
- **P3** — tenant onboarding / invitations / self-service user management.
- **P4** — the client-facing app/routes and hiding operator-only surfaces.
- **P5** — removing the CIDR gate, WAF, public edge hardening.
- **P6** — per-tenant PHI access reporting, per-org BAAs, export/delete, key management. (The isolation *test suite* in §7 starts here and grows in P6.)
- Billing / quotas / plans (possible later P7).
- Any UI change. P1 is schema + RLS + context plumbing + migration + tests only.

## 3. Architecture

### 3.1 `organizations` table

```
organizations(
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,        -- url/log-safe handle
  status      text not null default 'active',  -- active | suspended
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
)
```
RLS is **not** applied to this table (it is platform-level, read by the context resolver and, later, the act-as picker). Write access is platform-operator-only (enforced at the app layer in P2/P3).

### 3.2 `organization_id` on every tenant-owned table

Add `organization_id uuid NOT NULL REFERENCES organizations(id)` to all of the following (verified against `apps/api/src/usan_api/db/models.py`). The classification (PHI vs config) drives review priority, not behavior — every one gets the column and a policy:

**PHI:** `contacts`, `dnc_list`, `calls`, `transcripts`, `wellness_logs`, `medication_logs`, `medication_reminders`, `personal_facts`, `conversation_summaries`, `wellbeing_survey_results`, `activity_history`, `follow_up_flags`, `callback_requests`, `sms_messages`, `family_contacts`, `family_tasks`, `family_reports`.
**Per-call operational metrics (tenant-derived):** `turn_metrics`, `call_metrics`.
**Per-customer config:** `agent_profiles`, `agent_profile_versions`, `call_schedules`, `call_batches`, `call_batch_targets`, `webhook_endpoints`, `webhook_deliveries`, `custom_variables`. (Direction defaults are stored on `agent_profiles`/config rows; if a standalone defaults row exists at implementation time it is included.)
**Audit:** `admin_audit_log` — org-scoped so a client can later see its own trail; platform-level actions (act-as, cross-org admin) are recorded with the acting platform context.

**Stays global (no `organization_id`, no RLS):** `organizations` (new) and `admin_users` (gets `organization_id` in **P2**). The built-in voice catalog and variable catalog are code constants, not tables, so they are inherently shared.

**Denormalize onto child tables too.** Child tables (`transcripts`, `turn_metrics`, `call_metrics` → `calls`; `agent_profile_versions` → `agent_profiles`; `call_batch_targets` → `call_batches`; `family_tasks`/`family_reports` → family/contacts) carry their **own** `organization_id` rather than deriving it via join. Rationale: a per-table policy `organization_id = current_org` is simple, index-friendly, and can't be defeated by a missing join; RLS policies that join are slow and error-prone. The column is stamped at insert from the tenant context (§3.4), and a `WITH CHECK` clause makes a mismatched value impossible.

### 3.3 RLS policies (uniform, fail-closed)

For every tenant-owned table:
```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;   -- applies even to the table owner
CREATE POLICY tenant_isolation ON <t>
  USING      (organization_id = current_setting('app.current_org', true)::uuid)
  WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid);
```
- `current_setting('app.current_org', true)` returns NULL when unset → the predicate is `org = NULL` → **no rows match**. A query that forgets to set context returns nothing and an insert is rejected — **fail-closed**, never leak.
- `USING` governs read/update/delete visibility; `WITH CHECK` governs insert/update values — so a row can neither be read across orgs nor written into the wrong org.
- `FORCE ROW LEVEL SECURITY` ensures even the application's table-owner role is subject to the policy (otherwise RLS is bypassed for the owner).

### 3.4 Tenant-context plumbing

- The app's DB role is a normal (non-superuser, non-`BYPASSRLS`) role so RLS always applies.
- A request-scoped dependency wraps the existing `get_db` session: at the start of the transaction it runs `SET LOCAL app.current_org = :org`. **`SET LOCAL`** (transaction-scoped), never `SET`, so the value cannot leak across pooled connections between requests.
- Inserts: the repository layer stamps `organization_id` from the context; `WITH CHECK` is the backstop that rejects any mismatch.
- **In P1 (behavior-preserving), the resolver always returns the single default org**, so every existing caller (operator token, admin session, agent, webhooks) transparently operates in that one org. P2 replaces the resolver with "the authenticated user's org / the act-as target."

### 3.5 Background workers under RLS (explicit design point)

The schedule orchestrator, retry poller, webhook-delivery worker, and family-report job run **outside** a request and currently scan *all* due rows. Under RLS a worker with no context sees nothing (fail-closed). P1 handles this for the single-org world by **setting `app.current_org` to the default org** at the top of each worker's unit of work. The multi-org strategy (P2+) is specified as an interface, not built here: workers will either (a) iterate active orgs and set context per org, or (b) use a dedicated, tightly-scoped `BYPASSRLS` maintenance role *only* to enumerate due work, then set per-org context before acting on each item. See Open Question 1.

## 4. Migration & backfill (online, ordered, behavior-preserving)

One Alembic migration, in this order:
1. Create `organizations`.
2. Seed one default org row (id captured for backfill).
3. Add `organization_id` as **nullable** to every table in §3.2 (no rewrite-locking default).
4. Backfill: `UPDATE <t> SET organization_id = :default_org WHERE organization_id IS NULL` (batched for large tables: `calls`, `transcripts`, `turn_metrics`).
5. `ALTER ... SET NOT NULL` + add FK + add `organization_id` indexes (and composite indexes matching current hot filters, e.g. `(organization_id, phone_e164)` on `contacts`).
6. Enable + force RLS and create the policies (§3.3).
7. Wire the context dependency + worker context to the default org (§3.4/§3.5).

**Downgrade** drops policies → FK/indexes → columns → table, in reverse.

> **Assumption (confirm at spec review):** current prod `contacts`/`calls` are USAN's own pilot data, so the seeded default org = **"USAN Retirement"** (`slug: usan`). If that data is throwaway test data, seed an empty default and let it be reassigned.

## 5. Behavior preservation

After P1, the live system behaves exactly as today: a single implicit org, all data in it, RLS enforcing it. No login, API contract, or UI change. This is the safety property that lets us ship the bedrock migration confidently before any real second tenant exists — the isolation machinery is proven in tests against two synthetic orgs while production runs as one.

## 6. Components & boundaries

- **`organizations` model + repo** — CRUD used only by platform paths (seed in P1; managed in P3).
- **Tenant-context module** (`tenant_context.py`) — one place that (a) resolves the current org for a request/worker and (b) issues `SET LOCAL`. Single responsibility; P2 swaps the resolver without touching callers.
- **Migration** — the schema/RLS change, isolated in one Alembic revision.
- **Isolation test suite** — see §7.
No existing repository signatures change in P1 (they inherit isolation from RLS + the session context), keeping the diff focused on the foundation.

## 7. Isolation test suite (the safety net)

Non-negotiable acceptance criteria for P1:
- Seed two orgs A and B with overlapping data (same phone numbers, etc.).
- With context = A: every repository/list endpoint returns only A's rows; fetching B's row by primary key returns nothing; updating/deleting B's row affects zero rows; inserting with B's id while context=A is rejected by `WITH CHECK`.
- **RLS-is-the-backstop test:** run a raw query with the app-layer filter intentionally omitted and assert it *still* returns only A's rows — proving RLS, not just app code, enforces isolation.
- **No-context test:** with `app.current_org` unset, every tenant table returns zero rows and inserts fail (fail-closed).
- Worker test: a poller run under a given org's context only materializes/acts on that org's due rows.

## 8. Security / HIPAA notes

- RLS gives database-level "minimum necessary" enforcement and a clean auditor story: isolation does not depend on every developer remembering a `WHERE`.
- `FORCE RLS` + a non-`BYPASSRLS` app role means a compromised app cannot trivially read across tenants without also setting context (which is audited in P2).
- The act-as mechanism (P2) records who entered which org and when; P1 ensures the mechanism it relies on (context = chosen org) is the *only* way to see another org's data.
- Connection pooling correctness (`SET LOCAL`) is a security property here, not just hygiene — a leaked GUC across requests would be a cross-tenant leak.

## 9. Open questions

1. **Background-worker multi-org strategy (P2 decision, design-only here):** iterate-per-org vs a scoped `BYPASSRLS` enumerator role? Affects orchestrator throughput at many orgs. P1 only implements the single-org path.
2. **Audit visibility:** should a client org eventually see its *own* `admin_audit_log` rows (org-scoped, P4), while platform/act-as actions stay platform-only? P1 adds the column; the read surface is P4.
3. **`dnc_list` scope:** confirmed per-org (each client's own do-not-call list). Flagged in case a *global* regulatory DNC is also desired later (could be a separate global table).
4. **Default-org identity** — see §4 assumption.
