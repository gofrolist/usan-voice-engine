# Contacts timezone editor — surface & validate per-contact IANA timezone (design)

**Date:** 2026-06-15
**Status:** Approved (brainstorming) — pending implementation plan
**Branch:** `feat/contacts-timezone-editor`, off `origin/main` (tip `fd163ac`)
**Related specs:** `2026-06-07-admin-ui-design.md` (admin Contacts page), `2026-06-10-small-unlocks-design.md` (per-profile quiet-hours policy that consumes the contact timezone)

---

## 1. Problem

Every quiet-hours / scheduling computation resolves "local" time from the per-contact `contacts.timezone` column — it is the documented single source of truth (`db/models.py:644`). Callers pass `contact.timezone` into `ZoneInfo(...)`:

- Retries / callbacks: `quiet_hours.next_allowed(...)` — `schedule_orchestrator.py:547`, `callback_dialer.py:102`
- Scheduled / batch calls: `schedule_windows.next_run_at / window_bounds_utc / day_bounds_utc / local_date` — `schedule_orchestrator.py:128,307-340,378-380,561,578`

Two gaps:

1. **No UI to see or edit it.** The admin Contacts page (`apps/admin-ui/src/features/contacts/ContactsPage.tsx`) and its API (`routers/admin_contacts.py` → `ContactSummary`) expose only name / masked phone / assigned profile. An admin cannot tell which timezone a contact is in, nor correct a wrong one.
2. **Invalid zones are accepted on write, then fail closed forever.** Both contact write paths validate `timezone` only for `max_length` (`schemas/contact.py:14,23`). A bogus value (`"EST5"`, `"America/New_Yrok"`) saves fine, then makes `ZoneInfo(...)` raise at call time. The system correctly fails **closed** — `schedule_orchestrator` → `skipped_invalid_timezone` retried hourly (`:405-417`), batch target → `mark_target_skipped(reason="invalid_timezone")` (`:580-584`), callback left `open` (`callback_dialer.py:104-105`) — so no out-of-hours call is ever placed, but **every call to that contact is silently skipped** until a human notices.

## 2. Goals

- Add a **Timezone** column to the admin Contacts page with an inline editor (curated US-zone dropdown), gated to admins, audit-logged.
- Reject invalid IANA zones at **every** write boundary (the new admin endpoint **and** the existing operator API), turning the silent fail-closed into a loud 422 at write time.

## 3. Non-goals

- A per-contact override of the quiet-hours **window** (that lives on the profile policy — `2026-06-10-small-unlocks-design.md`). This feature only sets the *zone* the window is measured in.
- Auto-detecting a contact's timezone from area code or any inference. Admin/operator sets it explicitly.
- A full searchable all-IANA combobox in the UI. The dropdown is the curated US set; the backend still accepts any valid IANA zone (see §4.4) so exotic-but-valid existing values aren't boxed in.
- Bulk timezone edit / CSV import. One contact at a time, mirroring the existing per-row profile assign.
- Changing the fail-closed runtime behavior in `schedule_orchestrator` / `callback_dialer`. That stays as defense-in-depth; this feature prevents the bad data from being saved in the first place.

## 4. Architecture — backend (`apps/api`)

### 4.1 Shared IANA validator

`schemas/_validators.py` (already home to `E164_PATTERN`, `PHONE_MAX_LENGTH`) gains:

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONE_MAX_LENGTH = 64

def validate_iana_timezone(value: str) -> str:
    """Return value unchanged iff it names a resolvable IANA zone; else raise ValueError.

    Mirrors the fail-closed contract of quiet_hours._zone: a zone that won't
    construct must never reach the DB, where it would silently skip every call.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {value!r}") from exc
    return value
```

A pydantic `field_validator("timezone")` wrapper raising `ValueError` yields FastAPI's standard 422. The validator *is* `ZoneInfo(...)` construction, so by definition it accepts exactly what the runtime callers accept — zero drift from `quiet_hours._zone` / `schedule_windows._zone`. (Caveat for test authors: legacy compatibility names like `"EST5EDT"` / `"PST8PDT"` are real tzdata entries and **do** resolve; a truly invalid example is `"EST5"`, `""`, or `"Mars/Phobos"`.)

### 4.2 `schemas/admin.py`

- `ContactSummary` gains `timezone: str`.
- New request model:

```python
class SetTimezoneRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)
```

### 4.3 `repositories/contacts.py`

Add `set_timezone`, mirroring `assign_profile` (`:75-84`) — load, set, flush, return (caller commits):

```python
async def set_timezone(db, contact_id: uuid.UUID, timezone: str) -> Contact | None:
    contact = await db.get(Contact, contact_id)
    if contact is None:
        return None
    contact.timezone = timezone
    await db.flush()
    return contact
```

### 4.4 `routers/admin_contacts.py`

- `_summary(...)` (`:25-32`) includes `timezone=contact.timezone`.
- New endpoint mirroring `assign_profile` (`:46-76`):

```python
@router.put("/{contact_id}/timezone", response_model=ContactSummary)
async def set_timezone(
    contact_id, body: SetTimezoneRequest, db, actor=Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
):
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(404, "contact not found")
    old = contact.timezone
    contact = await contacts_repo.set_timezone(db, contact_id, body.timezone)
    await admin_audit.record(
        db, actor_email=actor, action="contact.set_timezone",
        entity_type="contact", entity_id=str(contact_id),
        detail={"old": old, "new": body.timezone},
    )
    await db.commit()
    # re-resolve profile name for the summary (same as assign_profile tail)
    ...
    return _summary(contact, profile_name)
```

Same `require_admin_role(ADMIN)` gate, same audit pattern, same `ContactSummary` response shape as profile-assign. The backend validator accepts **any** resolvable IANA zone, not just the curated UI set.

### 4.5 Operator-API hardening (`schemas/contact.py`)

Apply the same `field_validator("timezone")` to `ContactCreate.timezone` (`:14`) and `ContactUpdate.timezone` (`:23`). This is the path that actually populates the column today (`POST/PUT /v1/contacts`, `require_operator_token`). Behavior change: a create/update carrying an invalid zone now returns **422** instead of persisting a row that silently fails closed at call time. This is the intended fix, not a regression.

## 5. Architecture — frontend (`apps/admin-ui`)

### 5.1 Types — `types/api.ts`

- `ContactSummary` (`:260-266`) gains `timezone: string`.
- Add `SetTimezoneRequest { timezone: string }`.

### 5.2 Curated zone list — `features/contacts/timezones.ts`

```ts
export const US_TIMEZONES = [
  { value: "America/New_York",    label: "Eastern (America/New_York)" },
  { value: "America/Chicago",     label: "Central (America/Chicago)" },
  { value: "America/Denver",      label: "Mountain (America/Denver)" },
  { value: "America/Phoenix",     label: "Arizona — no DST (America/Phoenix)" },
  { value: "America/Los_Angeles", label: "Pacific (America/Los_Angeles)" },
  { value: "America/Anchorage",   label: "Alaska (America/Anchorage)" },
  { value: "Pacific/Honolulu",    label: "Hawaii (Pacific/Honolulu)" },
] as const;
```

### 5.3 Hook — `features/contacts/hooks.ts`

Add `useSetTimezone()` mirroring `useAssignProfile()` (PUT `/v1/admin/contacts/${id}/timezone`, `invalidateQueries(["contacts"])`, `onError: pushToast(err.detail)`). A 422 from §4.4 surfaces as a toast. Does **not** invalidate `["profiles"]` (timezone doesn't affect profile counts).

### 5.4 Page — `ContactsPage.tsx`

Add a **Timezone** `<Th>` and a `<Td>` with an inline `<Select>`, mirroring the profile select (`:73-100`):

- No empty option — `timezone` is `NOT NULL`, every contact has one.
- Value = `contact.timezone`. If it isn't one of `US_TIMEZONES`, prepend a preserved `<option value={tz}>{tz}</option>` so an exotic-but-valid current zone stays selected and visible — exactly the "(archived)" profile fallback at `:88-93`.
- Disable only the row being saved: `setTz.isPending && setTz.variables?.contactId === e.id`.
- The empty-state `colSpan` (`:64`) updates 3 → 4.

## 6. Testing

**API — `tests/test_admin_contacts_api.py`:**
- `ContactSummary` now serializes `timezone`.
- `PUT /timezone` happy path: valid zone persists, response echoes it, an `admin_audit` row with `action="contact.set_timezone"` and `{old,new}` detail is written.
- Invalid IANA → 422 (no DB write).
- Unknown contact id → 404.
- Non-admin session (VIEWER role) → 403 (role gate).

**Schema — `tests/test_contacts.py` (or `test_admin_contacts_schemas.py`):**
- `validate_iana_timezone` accepts `America/New_York`, rejects `"EST5"` / `""` / `"Mars/Phobos"`.
- `ContactCreate` / `ContactUpdate` now 422 on an invalid zone (regression guard for §4.5).

**UI — `apps/admin-ui/src/test/ContactsPage.test.tsx`:**
- Renders the contact's current zone as the selected option.
- Changing the select fires `useSetTimezone` with `{contactId, timezone}`.
- An exotic current value (e.g. `Europe/London`) renders as a visible selected option even though it's outside `US_TIMEZONES`.

(Per repo memory, run UI tests in isolation — full `vitest run` flakes on timeouts under parallel load.)

## 7. Security & auth

- The new endpoint reuses `require_admin_session` (router-level) + `require_admin_role(AdminRole.ADMIN)` (endpoint-level) — identical to profile-assign; grants no new authority surface.
- Audit trail: `contact.set_timezone` with before/after, consistent with `contact.assign_profile`.
- Operator-API change is validation-only (tighter input), no auth change.
- `mask_phone` is unaffected; no new PII surfaced (timezone is not PII here).

## 8. Open questions

1. Should the curated dropdown also offer a **"— other (keep current) —"** disabled hint when the value is exotic, or is the preserved raw-IANA option (§5.4) enough? *Leaning: preserved option is enough; no extra hint.*
2. Do any existing prod contacts already carry an invalid zone that §4.5's tighter `ContactUpdate` validator would now block on the next unrelated edit? *Mitigation: validator only runs on fields present in the request; `ContactUpdate.timezone` is optional, so an edit that doesn't touch timezone is unaffected. A one-off audit query of `contacts.timezone` against `ZoneInfo` before deploy is cheap insurance.*
