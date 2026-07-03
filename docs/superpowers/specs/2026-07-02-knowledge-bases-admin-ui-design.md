# Knowledge Bases — admin UI + native API (text-only v1)

**Date:** 2026-07-02
**Status:** Design (approved for planning)
**Depends on:** Phase 5 knowledge-bases (compat API, `knowledge_bases`/sources/chunks tables,
migration 0047), the KB ingestion poller (enabled in prod as of secret v9 / v0.14.0).

## Problem

Knowledge bases exist only behind the RetellAI-compat API (`POST /create-knowledge-base`,
etc.), which requires a super-admin-minted compat key and is served at root paths the admin
console cannot reach (Caddy proxies only `/v1/*`). There is no way for an org to manage its
own KB content from the admin UI. This feature adds a native, RLS-scoped admin surface for
creating knowledge bases and managing their **text** sources, so orgs can self-serve the
content that powers retrieval (RAG).

## Goals

- Org-admins create/delete knowledge bases and add/remove **text** sources from the admin UI.
- Reuse the existing `knowledge_bases` / sources / chunks tables and the running ingestion
  poller — a KB created here is identical to a compat KB and is embedded automatically.
- Show ingestion status so users can see a KB become searchable.
- RLS-isolated per org; viewers read-only; admins write. (Mirrors Profiles/Defaults/Variables.)

## Non-goals (v1)

- **File upload** (PDF/DOCX). Text-only for v1; the backend already rejects files/URLs with
  `422 "only text sources are supported"`. File ingestion (parse → extract → embed) is a
  tracked follow-up.
- **URL sources**, auto-refresh, and chunk-size configuration UI.
- **Binding a KB to an agent.** Retrieval only fires when an agent references a KB id in its
  config; that lives with agent configuration and is a separate step. See "RAG activation".
- Changing retrieval behavior or flags (`KB_RETRIEVAL_ENABLED` stays a deploy/ops concern).

## Architecture

### Chosen approach
A native `/v1/admin/knowledge-bases` router that reuses the `repositories/knowledge_bases.py`
repository and the existing tables directly. One data model shared with the compat surface;
the already-running poller embeds new/changed sources with no extra wiring.

Rejected alternatives:
- **Proxy to the compat handlers** — couples the admin plane to compat multipart encoding,
  JSON-string blob fields, and API-key auth. Messy, leaky abstraction.
- **Expose the compat plane to the admin origin** — changes the compat security model
  (compat is bearer-key auth, deliberately not session-cookie/RLS). Rejected.

### Backend

New router `apps/api/src/usan_api/routers/admin_knowledge_bases.py`, mounted under
`/v1/admin/knowledge-bases`, included in `main.py` alongside the other admin routers.

Authz + isolation (mirrors `admin_profiles` / org-admin authoring):
- All routes `Depends(require_admin_role(...))` — reads gate on `VIEWER`, writes on `ADMIN`.
- Session-cookie auth, RLS tenant context set per request (same as other `/v1/admin/*`).
- No cross-org access; a KB id from another org resolves to 404 under RLS.

Native Pydantic schemas (`schemas/admin_knowledge_bases.py`) use **raw UUIDs** (like other
admin routes), not the compat `kb_`/`source_`-encoded tokens.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/v1/admin/knowledge-bases` | VIEWER | List KBs: id, name, status, source_count, updated_at |
| POST | `/v1/admin/knowledge-bases` | ADMIN | Create (name only); KB starts `in_progress` (poller-claimable) |
| GET | `/v1/admin/knowledge-bases/{id}` | VIEWER | KB detail + its sources (id, title, status) |
| DELETE | `/v1/admin/knowledge-bases/{id}` | ADMIN | Delete KB (+ sources/chunks via cascade) |
| POST | `/v1/admin/knowledge-bases/{id}/sources` | ADMIN | Add text source (title, text) → reset KB to `in_progress` |
| DELETE | `/v1/admin/knowledge-bases/{id}/sources/{source_id}` | ADMIN | Remove a source |

Shared logic: extract the "persist a text source + reset the KB to `in_progress` for
(re)ingestion" step out of the compat `kb_service` into a small helper (e.g.
`compat/kb_sources.py::add_text_sources(db, kb_id, texts)`), and have **both** the compat
`kb_service.add_sources` and the native router call it. Single source of truth for how adding
a source triggers (re)ingestion; prevents the two surfaces from drifting. Validation
(non-empty name/title/text, chunk-size bounds where relevant) stays shared too.

Ingestion is unchanged and matches the compat path: `create_kb` sets status **`in_progress`**;
the `kb_ingestion_poller` (already enabled in prod) lease-claims `in_progress` KBs, embeds their
sources via Vertex `text-embedding-005`, writes chunks, and sets status **`complete`** (or
**`error`** after bounded retries). An empty KB (no sources) simply completes with no chunks.
Adding a source resets the KB to `in_progress` so the poller re-ingests. The admin API only
reads status and resets it via the shared helper; it never embeds inline.

### Frontend

New page `apps/admin-ui/src/features/knowledgeBases/` + a route/nav entry under the **Config**
group (admin-gated visibility consistent with Contacts/Schedules).

- **List view:** name, a status badge (in progress / complete / error — reuse the
  `Badge` primitive with tones), source count, updated time. "New knowledge base" (admin only).
- **Create dialog:** name field → POST → navigate to detail.
- **Detail view:** KB name + status; a sources list (title, per-source status, delete); an
  "Add text source" form (title input + text `textarea`). Admin-only controls; viewers see a
  read-only list.
- **Live status:** while any KB is `in_progress`, react-query `refetchInterval` polls (a few
  seconds) so the badge flips to `complete` without a manual refresh; polling stops when nothing
  is in flight.
- react-query hooks in `features/knowledgeBases/hooks.ts`; types in `types/api.ts`; role
  gating via `useIsAdmin()`.

### Data flow

```
Admin UI  --POST /v1/admin/knowledge-bases/{id}/sources-->  native router (RLS, ADMIN)
        --> add_text_sources() [shared helper] --> repo.add_source + set KB status=in_progress
KB ingestion poller (running) --> claim_pending (leases in_progress) --> Vertex embed
        --> insert_chunks --> status=complete
Admin UI polls GET /{id} --> sees status=complete
```

## Error handling

- Missing/empty name, title, or text → `422` with a clear message (shared validation).
- Cross-org / unknown id → `404` (RLS-scoped lookup; never leak other orgs' existence).
- Viewer attempting a write → `403` (role gate).
- Ingestion failures are surfaced as the KB/source `error` status in the UI (with the stored
  error reason if present), not as an API error on the management calls.
- Deleting a KB mid-ingestion is safe: delete cascades; a poller mid-cycle on a deleted KB
  no-ops (row gone under its RLS scope).

## Testing

Backend (`apps/api/tests/`):
- Router tests: create/list/get/delete KB; add/delete source; response shapes.
- RLS isolation: an admin in org A cannot see/mutate org B's KB (404).
- Role gating: viewer read-only (403 on writes); admin full.
- Status: adding a source resets the KB to `in_progress`; the shared helper is exercised by both
  the native router and the existing compat path (no regression to compat KB tests).

Frontend (`apps/admin-ui/src/test/`):
- List renders KBs with status badges; create dialog posts and shows the new KB.
- Add-source form posts title+text; delete source/KB confirm flows.
- Role gating: viewer sees no write controls; admin does.
- Polling: an `in_progress` KB refetches and flips to complete (mocked).

Coverage target 80%+ per repo standard; follow existing admin-ui/api test patterns
(route-by-URL api mock, `meFixture`, testcontainer DB for RLS).

## RAG activation (context, not part of this feature)

After this ships, getting retrieval live end-to-end still requires, as separate steps:
1. Load content through this UI (KB → `complete`).
2. Bind the KB to an agent (agent config; a tracked follow-up).
3. Enable retrieval flags in prod (`KB_RETRIEVAL_ENABLED`, and `KB_RETRIEVAL_VOICE_ENABLED`
   for voice) — an ops step, secret + `.env` + recreate, already wired via v0.14.0.

## Rollout

- No new migration (reuses migration 0047 tables).
- New native router is inert until called; ships behind the standard admin auth. Merges as its
  own squash PR; visible after the next `v*` tag deploy.
- The KB ingestion poller is already enabled in prod, so content added via the new UI ingests
  immediately once deployed.
