# Contract: New/Changed Admin & Operator Endpoints

These support operator management of the new entities. They follow the existing admin router conventions (`routers/admin_*.py`): `require_admin_session`, role-gated mutations, audit-logged, Pydantic v2 bodies. Operator-facing UI for these is a thin follow-on; the APIs are part of this plan so the behaviors are usable and testable.

---

## Family contacts & tasks

### POST `/v1/admin/family-contacts`
Create a family contact for an elder.
Request: `{ "elder_id": "<uuid>", "name": "Maria", "phone_e164": "+15551234567", "relationship": "daughter", "alert_prefs": { "missed": true, "crisis": true, "report": true } }`
Response: full FamilyContact.

### GET `/v1/admin/family-contacts?elder_id=<uuid>`
List family contacts for an elder.

### PATCH `/v1/admin/family-contacts/{id}` / DELETE `/v1/admin/family-contacts/{id}`
Update relationship/phone/alert prefs; remove.

### GET `/v1/admin/family-tasks?elder_id=&status=`
List family tasks (open/delivered/closed/needs_review), urgent/needs_review first.

### PATCH `/v1/admin/family-tasks/{id}`
Operator transitions: approve a `needs_review` task → `open`, or close. Body: `{ "status": "open|closed" }`.

---

## Crisis escalations (extends existing flag surface)

### GET `/v1/admin/follow-up-flags?severity=urgent&crisis=true`
The existing flag list, now returning crisis columns (`crisis_category`, `detection_source`, `resource_offered`, `family_notified`). No new endpoint shape beyond the added response fields + filter.

---

## Schedules (evening slot)

### POST `/v1/schedules` / PATCH `/v1/schedules/{id}` (extended)
Add `slot` (`morning|evening`, default `morning`) to the request/response. `GET /v1/schedules?elder_id=` now returns up to two rows per elder. Creating a second schedule for the same `(elder_id, slot)` is the only uniqueness conflict (was previously per-elder).

---

## Callback requests (auto-dial visibility)

### GET `/v1/admin/callback-requests?status=`
Now includes `scheduled`/`dialed` statuses and `dispatched_call_id`, so operators can see which callbacks the auto-dialer placed.

---

## Family reports

### GET `/v1/admin/family-reports?elder_id=`
List generated monthly reports (period, delivery status, linked SMS). Read-only; generation is a poller job.

### POST `/v1/admin/family-reports/{id}/resend` (optional, role-gated)
Re-enqueue the report SMS (idempotent).

---

## Survey & activity history (read)

### GET `/v1/admin/elders/{id}/wellbeing` (aggregate read)
Returns recent survey results + activity history + medication-reminder state for an elder, for operator review and to back the monthly report. Read-only.

---

## Notes
- All mutations audited via the existing `admin_audit_log` (no PHI in detail).
- No changes to existing endpoint contracts except additive response fields (`slot`, crisis columns, callback `dispatched_call_id`) — backward compatible.
