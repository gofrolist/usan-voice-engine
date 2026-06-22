# Contract — Compat API Key Administration (native `/v1/admin`)

These endpoints are **not** part of the RetellAI surface. They live on the **native** `/v1/admin`
plane, are guarded by the existing admin session + **super-admin** role, and issue/list/revoke the
Bearer keys the CRM uses against the compat surface (FR-003). Errors use the native FastAPI
`{detail}` envelope (not the compat `{status,message}` envelope).

## POST `/v1/admin/compat-keys` → 201
Issue a new key for an organization.

Request: `{ "organization_id": "<uuid>", "label": "crm-prod" }`
Response (**token shown once**):
```json
{ "id": "<uuid>", "organization_id": "<uuid>", "label": "crm-prod",
  "key_prefix": "key_ab12", "api_key": "key_<full-token>", "created_at": "<iso8601>" }
```
The full `api_key` is returned **only here** and is never readable again (mirrors the webhook
signing-secret pattern). Store `key_hash` = sha256(api_key); persist `key_prefix` for display/lookup.

## GET `/v1/admin/compat-keys` → 200
List keys for the active org (no secrets): `[{ id, organization_id, label, key_prefix, status,
created_at, last_used_at, revoked_at }]`.

## DELETE `/v1/admin/compat-keys/{id}` → 204
Revoke a key (sets `status="revoked"`, `revoked_at=now`). Subsequent compat requests with that key →
**401**. Revocation is immediate (the auth dependency checks `status="active"` on every request).

---

**Auth model note**: super-admin issues keys per organization. The key's `organization_id` selects
the RLS tenant context for every compat request authenticated with it — so a key only ever sees its
own org's calls/agents/batches (verified by `test_compat_rls_isolation.py`).
