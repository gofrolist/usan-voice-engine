# Contacts Timezone Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin see and correct each contact's IANA timezone from the admin Contacts page, and reject invalid timezones at every write boundary so a bad zone can never silently skip all of a contact's calls.

**Architecture:** A shared `validate_iana_timezone` helper (constructs `ZoneInfo`) is applied as a pydantic `field_validator` on the existing operator-API contact schemas and a new admin `SetTimezoneRequest`. A new admin endpoint `PUT /v1/admin/contacts/{id}/timezone` (admin-role gated, audit-logged) mirrors the existing profile-assign endpoint. The Contacts page gains a Timezone column with an inline curated-US-zone `<Select>`, mirroring the existing profile `<Select>`.

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy async (apps/api, Python 3.14, uv); React + TanStack Query + Vitest (apps/admin-ui).

**Spec:** `docs/superpowers/specs/2026-06-15-contacts-timezone-editor-design.md`

**Branch:** `feat/contacts-timezone-editor` (already created off `main`).

**Working directories:** API commands run from `apps/api`; UI commands run from `apps/admin-ui`.

---

## Task 1: Shared IANA timezone validator

**Files:**
- Modify: `apps/api/src/usan_api/schemas/_validators.py`
- Test: `apps/api/tests/test_schema_validators.py` (create)

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_schema_validators.py`:

```python
import pytest

from usan_api.schemas._validators import (
    TIMEZONE_MAX_LENGTH,
    validate_iana_timezone,
)


def test_valid_iana_zone_returned_unchanged():
    assert validate_iana_timezone("America/New_York") == "America/New_York"
    assert validate_iana_timezone("Pacific/Honolulu") == "Pacific/Honolulu"


@pytest.mark.parametrize("bad", ["EST5", "", "Mars/Phobos", "america/new_york "])
def test_invalid_iana_zone_raises_valueerror(bad):
    with pytest.raises(ValueError):
        validate_iana_timezone(bad)


def test_timezone_max_length_constant():
    assert TIMEZONE_MAX_LENGTH == 64
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_schema_validators.py -v`
Expected: FAIL with `ImportError: cannot import name 'validate_iana_timezone'`.

- [ ] **Step 3: Write minimal implementation**

Append to `apps/api/src/usan_api/schemas/_validators.py`:

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Generous upper bound for an IANA zone name (longest real names are ~30 chars).
TIMEZONE_MAX_LENGTH = 64


def validate_iana_timezone(value: str) -> str:
    """Return ``value`` unchanged iff it names a resolvable IANA zone; else ValueError.

    Identical construction to ``quiet_hours._zone`` / ``schedule_windows._zone`` —
    so anything this accepts the runtime callers also accept (zero drift). A zone
    that won't construct must never reach the DB, where it would silently skip
    every call to that contact.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {value!r}") from exc
    return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_schema_validators.py -v`
Expected: PASS (all 6 cases).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/schemas/_validators.py apps/api/tests/test_schema_validators.py
git commit -m "feat(api): add shared validate_iana_timezone helper"
```

---

## Task 2: Harden operator-API contact schemas

**Files:**
- Modify: `apps/api/src/usan_api/schemas/contact.py:11-26`
- Test: `apps/api/tests/test_contacts_backcompat.py` (add cases)

> Note: the test file already exists (`test_contacts_backcompat.py`). Add the new tests to it; do not create a new file.

- [ ] **Step 1: Write the failing test**

Add to `apps/api/tests/test_contacts_backcompat.py`:

```python
import pytest
from pydantic import ValidationError

from usan_api.schemas.contact import ContactCreate, ContactUpdate


def test_contact_create_rejects_invalid_timezone():
    with pytest.raises(ValidationError):
        ContactCreate(name="X", phone_e164="+15551230001", timezone="Mars/Phobos")


def test_contact_create_accepts_valid_timezone():
    c = ContactCreate(name="X", phone_e164="+15551230001", timezone="America/Chicago")
    assert c.timezone == "America/Chicago"


def test_contact_update_rejects_invalid_timezone():
    with pytest.raises(ValidationError):
        ContactUpdate(timezone="EST5")


def test_contact_update_allows_omitted_timezone():
    # Omitting timezone is the common partial-update case; must not trip the validator.
    u = ContactUpdate(name="New Name")
    assert u.timezone is None
```

(If `import pytest` / `from pydantic import ValidationError` already exist at the top of the file, don't duplicate them.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_contacts_backcompat.py -v -k timezone`
Expected: FAIL — `ContactCreate(timezone="Mars/Phobos")` does NOT raise (validator not wired yet).

- [ ] **Step 3: Write minimal implementation**

Replace the `ContactCreate` and `ContactUpdate` classes (and update the import line) in `apps/api/src/usan_api/schemas/contact.py`. The current import is `from usan_api.schemas._validators import E164_PATTERN, PHONE_MAX_LENGTH`; change it and the classes to:

```python
from pydantic import BaseModel, Field, field_validator

from usan_api.schemas._validators import (
    E164_PATTERN,
    PHONE_MAX_LENGTH,
    TIMEZONE_MAX_LENGTH,
    validate_iana_timezone,
)


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=255)
    preferred_voice: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone_e164: str | None = Field(default=None, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str | None = Field(default=None, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=255)
    preferred_voice: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str | None) -> str | None:
        # Optional: only validate a provided value; omission/None is a no-op so a
        # partial update that doesn't touch timezone is unaffected.
        return None if v is None else validate_iana_timezone(v)
```

Keep the existing `import uuid`, `from datetime import datetime`, `from typing import Any` lines and the `ContactResponse` class below unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_contacts_backcompat.py -v`
Expected: PASS (new timezone cases + all pre-existing cases in the file).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/schemas/contact.py apps/api/tests/test_contacts_backcompat.py
git commit -m "feat(api): reject invalid IANA timezone on contact create/update"
```

---

## Task 3: Admin schema — ContactSummary.timezone + SetTimezoneRequest

**Files:**
- Modify: `apps/api/src/usan_api/schemas/admin.py:1-28`
- Test: covered by the integration tests in Task 5 (no standalone test for this task).

- [ ] **Step 1: Edit the schema**

In `apps/api/src/usan_api/schemas/admin.py`, change the import line `from pydantic import BaseModel` to:

```python
from pydantic import BaseModel, Field, field_validator

from usan_api.schemas._validators import TIMEZONE_MAX_LENGTH, validate_iana_timezone
```

Add `timezone` to `ContactSummary` (the field is `NOT NULL` in the DB, so it is required here):

```python
class ContactSummary(BaseModel):
    id: uuid.UUID
    name: str
    masked_phone: str
    timezone: str
    agent_profile_id: uuid.UUID | None = None
    agent_profile_name: str | None = None
```

Add the request model after `AssignProfileRequest`:

```python
class SetTimezoneRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `cd apps/api && uv run python -c "from usan_api.schemas.admin import ContactSummary, SetTimezoneRequest; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/schemas/admin.py
git commit -m "feat(api): add timezone to ContactSummary + SetTimezoneRequest schema"
```

---

## Task 4: Repository — set_timezone

**Files:**
- Modify: `apps/api/src/usan_api/repositories/contacts.py` (append after `assign_profile`, ~line 85)
- Test: covered by the integration tests in Task 5.

- [ ] **Step 1: Add the function**

Append to `apps/api/src/usan_api/repositories/contacts.py`:

```python
async def set_timezone(
    db: AsyncSession, contact_id: uuid.UUID, timezone: str
) -> Contact | None:
    """Set a contact's IANA timezone. Returns None if the contact is gone. Caller commits."""
    contact = await db.get(Contact, contact_id)
    if contact is None:
        return None
    contact.timezone = timezone
    await db.flush()
    return contact
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `cd apps/api && uv run python -c "from usan_api.repositories.contacts import set_timezone; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/repositories/contacts.py
git commit -m "feat(api): add contacts_repo.set_timezone"
```

---

## Task 5: Admin endpoint — PUT /v1/admin/contacts/{id}/timezone

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_contacts.py`
- Test: `apps/api/tests/test_admin_contacts_api.py` (add cases)

- [ ] **Step 1: Write the failing tests**

Add to the top imports of `apps/api/tests/test_admin_contacts_api.py` (the file currently imports `asyncio`, `uuid`, and sqlalchemy bits):

```python
from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings
```

Add this local helper near `_seed_contact` (seeds an admin_users row with a role, mirroring `test_admin_users_api._seed`):

```python
async def _seed_admin(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "r": role},
            )
    finally:
        await engine.dispose()
```

Add these tests at the end of the file:

```python
def test_summary_includes_timezone(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Shown", "+15551250001"))
    listed = client.get("/v1/admin/contacts").json()
    me = next(e for e in listed if e["id"] == eid)
    assert me["timezone"] == "America/New_York"


def test_set_timezone_happy_path_and_audit(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Set", "+15551250002"))
    r = client.put(f"/v1/admin/contacts/{eid}/timezone", json={"timezone": "America/Chicago"})
    assert r.status_code == 200
    assert r.json()["timezone"] == "America/Chicago"
    # Persisted: a re-list reflects the new zone.
    listed = client.get("/v1/admin/contacts").json()
    assert next(e for e in listed if e["id"] == eid)["timezone"] == "America/Chicago"
    # Audited with before/after.
    rows = client.get("/v1/admin/audit?action=contact.set_timezone").json()
    entry = next(e for e in rows if e["entity_id"] == eid)
    assert entry["detail"] == {"old": "America/New_York", "new": "America/Chicago"}


def test_set_timezone_invalid_iana_422_leaves_value_unchanged(
    client, admin_session, async_database_url
):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Bad", "+15551250003"))
    r = client.put(f"/v1/admin/contacts/{eid}/timezone", json={"timezone": "Mars/Phobos"})
    assert r.status_code == 422
    listed = client.get("/v1/admin/contacts").json()
    assert next(e for e in listed if e["id"] == eid)["timezone"] == "America/New_York"


def test_set_timezone_unknown_contact_404(client, admin_session):
    r = client.put(
        f"/v1/admin/contacts/{uuid.uuid4()}/timezone", json={"timezone": "America/Chicago"}
    )
    assert r.status_code == 404


def test_viewer_cannot_set_timezone(client, async_database_url):
    eid = asyncio.run(_seed_contact(async_database_url, "Tz Viewer", "+15551250004"))
    asyncio.run(_seed_admin(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = client.put(f"/v1/admin/contacts/{eid}/timezone", json={"timezone": "America/Chicago"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_admin_contacts_api.py -v -k "timezone"`
Expected: FAIL — `test_summary_includes_timezone` KeyErrors on `"timezone"` and the PUT cases 404/405 (endpoint not defined).

- [ ] **Step 3: Implement the summary field + endpoint**

In `apps/api/src/usan_api/routers/admin_contacts.py`:

(a) Update `_summary` (`:25-32`) to include timezone:

```python
def _summary(contact: Contact, profile_name: str | None) -> ContactSummary:
    return ContactSummary(
        id=contact.id,
        name=contact.name,
        masked_phone=mask_phone(contact.phone_e164),
        timezone=contact.timezone,
        agent_profile_id=contact.agent_profile_id,
        agent_profile_name=profile_name,
    )
```

(b) Update the import of admin schemas (`:16`) to include the new request model:

```python
from usan_api.schemas.admin import AssignProfileRequest, ContactSummary, SetTimezoneRequest
```

(c) Append the endpoint after `assign_profile`:

```python
@router.put("/{contact_id}/timezone", response_model=ContactSummary)
async def set_timezone(
    contact_id: uuid.UUID,
    body: SetTimezoneRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactSummary:
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    old = contact.timezone
    contact = await contacts_repo.set_timezone(db, contact_id, body.timezone)
    assert contact is not None  # just fetched it above under the same session
    await admin_audit.record(
        db,
        actor_email=actor,
        action="contact.set_timezone",
        entity_type="contact",
        entity_id=str(contact_id),
        detail={"old": old, "new": body.timezone},
    )
    await db.commit()
    profile_name = None
    if contact.agent_profile_id is not None:
        prof = await profiles_repo.get_profile(db, contact.agent_profile_id)
        profile_name = prof.name if prof else None
    return _summary(contact, profile_name)
```

(No new imports needed beyond the schema: `require_admin_role`, `AdminRole`, `get_actor_email`, `admin_audit`, `profiles_repo`, `contacts_repo`, `HTTPException`, `status`, `Depends`, `AsyncSession`, `get_db` are all already imported in this file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_admin_contacts_api.py -v`
Expected: PASS — the new timezone cases plus all pre-existing contacts tests.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_contacts.py apps/api/tests/test_admin_contacts_api.py
git commit -m "feat(api): admin endpoint to set contact timezone (audited, admin-gated)"
```

---

## Task 6: API lint, format, types — green gate

**Files:** none (verification only).

- [ ] **Step 1: Ruff + format + mypy**

Run:
```bash
cd apps/api && ruff check . && ruff format --check . && uv run mypy
```
Expected: ruff clean; mypy reports no new errors. (CI runs mypy — see repo memory `ci-runs-mypy`. The `assert contact is not None` is intentional type narrowing and should satisfy mypy.)

- [ ] **Step 2: Full API test suite**

Run: `cd apps/api && uv run pytest -q`
Expected: PASS (no regressions in contacts, schedules, callbacks, batches).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A && git commit -m "chore(api): lint/format/type fixups for timezone editor" || echo "nothing to commit"
```

---

## Task 7: Frontend types — ContactSummary.timezone + SetTimezoneRequest

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts:260-270`

- [ ] **Step 1: Edit the types**

In `apps/admin-ui/src/types/api.ts`, update `ContactSummary` and add the request type:

```typescript
export interface ContactSummary {
  id: string;
  name: string;
  masked_phone: string;
  timezone: string;
  agent_profile_id: string | null;
  agent_profile_name: string | null;
}

export interface AssignProfileRequest {
  agent_profile_id: string | null;
}

export interface SetTimezoneRequest {
  timezone: string;
}
```

- [ ] **Step 2: Typecheck**

Run: `cd apps/admin-ui && npx tsc --noEmit`
Expected: PASS (this change alone introduces no type errors).

- [ ] **Step 3: Commit**

```bash
git add apps/admin-ui/src/types/api.ts
git commit -m "feat(admin-ui): add timezone to ContactSummary type"
```

---

## Task 8: Frontend — curated US timezone list

**Files:**
- Create: `apps/admin-ui/src/features/contacts/timezones.ts`

- [ ] **Step 1: Create the constant**

Create `apps/admin-ui/src/features/contacts/timezones.ts`:

```typescript
// Curated US IANA zones for the Contacts editor. The backend accepts ANY valid
// IANA zone (see schemas/_validators.validate_iana_timezone), so an existing
// out-of-list value is preserved in the UI rather than forced into this set.
export interface TimezoneOption {
  value: string;
  label: string;
}

export const US_TIMEZONES: readonly TimezoneOption[] = [
  { value: "America/New_York", label: "Eastern (America/New_York)" },
  { value: "America/Chicago", label: "Central (America/Chicago)" },
  { value: "America/Denver", label: "Mountain (America/Denver)" },
  { value: "America/Phoenix", label: "Arizona — no DST (America/Phoenix)" },
  { value: "America/Los_Angeles", label: "Pacific (America/Los_Angeles)" },
  { value: "America/Anchorage", label: "Alaska (America/Anchorage)" },
  { value: "Pacific/Honolulu", label: "Hawaii (Pacific/Honolulu)" },
];
```

- [ ] **Step 2: Typecheck**

Run: `cd apps/admin-ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/admin-ui/src/features/contacts/timezones.ts
git commit -m "feat(admin-ui): curated US timezone option list"
```

---

## Task 9: Frontend — useSetTimezone hook

**Files:**
- Modify: `apps/admin-ui/src/features/contacts/hooks.ts`

- [ ] **Step 1: Add the hook**

Append to `apps/admin-ui/src/features/contacts/hooks.ts` (the file already imports `useMutation`, `useQueryClient`, `api`, `ApiError`, `pushToast`, `ContactSummary`, and defines `CONTACTS_KEY`):

```typescript
interface SetTimezoneVars {
  contactId: string;
  timezone: string;
}

export function useSetTimezone() {
  const qc = useQueryClient();
  return useMutation<ContactSummary, ApiError, SetTimezoneVars>({
    mutationFn: ({ contactId, timezone }) =>
      api.put<ContactSummary>(`/v1/admin/contacts/${contactId}/timezone`, { timezone }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
    },
    // A 422 (invalid IANA zone) from the API surfaces as a toast.
    onError: (err) => pushToast(err.detail),
  });
}
```

- [ ] **Step 2: Typecheck**

Run: `cd apps/admin-ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/admin-ui/src/features/contacts/hooks.ts
git commit -m "feat(admin-ui): useSetTimezone mutation hook"
```

---

## Task 10: Frontend — Timezone column + editor on ContactsPage (with test)

**Files:**
- Create: `apps/admin-ui/src/test/ContactsPage.test.tsx`
- Modify: `apps/admin-ui/src/features/contacts/ContactsPage.tsx`

- [ ] **Step 1: Write the failing test**

Create `apps/admin-ui/src/test/ContactsPage.test.tsx` (mirrors `QueuesPage.test.tsx`: route-by-URL api mock, real `useIsAdmin` driven by `/v1/auth/me`):

```typescript
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ContactSummary } from "../types/api";

const getMock = vi.fn();
const putMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    put: (u: string, b?: unknown) => putMock(u, b),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
}));

import { ContactsPage } from "../features/contacts/ContactsPage";

let contacts: ContactSummary[] = [];

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve({ email: "me@example.com", role: "admin" });
  if (url.startsWith("/v1/admin/contacts")) return Promise.resolve(contacts);
  if (url.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ContactsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function contact(over: Partial<ContactSummary> = {}): ContactSummary {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Edna Moore",
    masked_phone: "***4567",
    timezone: "America/New_York",
    agent_profile_id: null,
    agent_profile_name: null,
    ...over,
  };
}

describe("ContactsPage timezone editor", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
    contacts = [contact()];
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the contact's current timezone as the selected option", async () => {
    renderPage();
    const select = await screen.findByLabelText("Timezone for Edna Moore");
    expect((select as HTMLSelectElement).value).toBe("America/New_York");
  });

  it("calls the API when the timezone is changed", async () => {
    putMock.mockResolvedValue(contact({ timezone: "America/Chicago" }));
    renderPage();
    const select = await screen.findByLabelText("Timezone for Edna Moore");
    await userEvent.selectOptions(select, "America/Chicago");
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith(
        "/v1/admin/contacts/11111111-1111-1111-1111-111111111111/timezone",
        { timezone: "America/Chicago" },
      ),
    );
  });

  it("keeps an exotic (non-US) current zone visible and selected", async () => {
    contacts = [contact({ timezone: "Europe/London" })];
    renderPage();
    const select = await screen.findByLabelText("Timezone for Edna Moore");
    expect((select as HTMLSelectElement).value).toBe("Europe/London");
    expect(screen.getByRole("option", { name: "Europe/London" })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactsPage.test.tsx`
Expected: FAIL — no element with label `Timezone for Edna Moore` (column not added yet).

- [ ] **Step 3: Implement the column + editor**

In `apps/admin-ui/src/features/contacts/ContactsPage.tsx`:

(a) Add the timezone-list import near the other imports, and extend the hooks import:

```typescript
import { US_TIMEZONES } from "./timezones";
import { useAssignProfile, useContacts, useSetTimezone } from "./hooks";
```

(replace the existing `import { useAssignProfile, useContacts } from "./hooks";` line.)

(b) Add the mutation hook after `const assign = useAssignProfile();`:

```typescript
  const setTz = useSetTimezone();
```

(c) Change the `<Thead>` row to add a Timezone header:

```tsx
          <Tr>
            <Th>Name</Th>
            <Th>Phone</Th>
            <Th>Timezone</Th>
            <Th>Assigned profile</Th>
          </Tr>
```

(d) Update the empty-state `colSpan` from `3` to `4`.

(e) Add a `<Td>` with the timezone `<Select>` between the phone cell and the profile cell:

```tsx
              <Td>
                <Select
                  aria-label={`Timezone for ${e.name}`}
                  value={e.timezone}
                  disabled={setTz.isPending && setTz.variables?.contactId === e.id}
                  onChange={(ev) =>
                    setTz.mutate({ contactId: e.id, timezone: ev.target.value })
                  }
                >
                  {/* Keep a current value that isn't in the curated US set visible. */}
                  {!US_TIMEZONES.some((tz) => tz.value === e.timezone) ? (
                    <option value={e.timezone}>{e.timezone}</option>
                  ) : null}
                  {US_TIMEZONES.map((tz) => (
                    <option key={tz.value} value={tz.value}>
                      {tz.label}
                    </option>
                  ))}
                </Select>
              </Td>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactsPage.test.tsx`
Expected: PASS (all 3 cases).

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/features/contacts/ContactsPage.tsx apps/admin-ui/src/test/ContactsPage.test.tsx
git commit -m "feat(admin-ui): timezone column + inline editor on Contacts page"
```

---

## Task 11: Frontend green gate

**Files:** none (verification only).

- [ ] **Step 1: Lint + typecheck**

Run: `cd apps/admin-ui && npm run lint && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 2: Run the new test in isolation**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactsPage.test.tsx`
Expected: PASS. (Per repo memory `admin-ui-test-flakiness`, prefer targeted files; the full parallel suite flakes on 5000ms timeouts. If you do run the full suite and 1-3 unrelated tests time out, re-run them in isolation before treating it as a regression.)

- [ ] **Step 3: Commit any fixups**

```bash
git add -A && git commit -m "chore(admin-ui): lint/type fixups for timezone editor" || echo "nothing to commit"
```

---

## Task 12: Pre-deploy data audit (operational note, no code)

The Task 2 tightening means a future `ContactUpdate` carrying `timezone` will 422 on a bad zone. It does NOT retroactively touch existing rows (the validator runs only on provided fields). Before deploying, sanity-check that no existing prod contact already holds an unresolvable zone (those are already failing closed at call time):

- [ ] **Step 1: Audit existing zones (read-only)**

For the operator to run against prod Cloud SQL (read-only): `SELECT DISTINCT timezone FROM contacts;` and confirm each value resolves via Python `zoneinfo.ZoneInfo`. Any that don't were already silently skipping calls and should be corrected via the new editor after deploy. Record findings in the PR description. (No code change in this task.)

---

## Final verification before PR

- [ ] All commits present: `git log --oneline main..HEAD`
- [ ] API: `cd apps/api && ruff check . && uv run mypy && uv run pytest -q` → green
- [ ] UI: `cd apps/admin-ui && npm run lint && npx tsc --noEmit && npx vitest run src/test/ContactsPage.test.tsx` → green
- [ ] Manual smoke (optional, needs a running stack): open the Contacts page, change a contact's timezone, confirm the select persists after reload and an audit row appears under System → Audit.
- [ ] Open PR `feat/contacts-timezone-editor` → `main`; paste the Task 2 behavior-change note and the Task 12 audit findings into the PR body.
