# Admin Call-Orchestration Backend (PR A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the call-orchestration plane (contacts CRUD, schedules, ad-hoc "call now", DNC) to org admins through new SSO-gated, RLS-isolated `/v1/admin/*` endpoints, leaving the operator-token machine plane untouched.

**Architecture:** Extract the call enqueue/dispatch core and the schedule create/update logic out of the operator routers into shared `services/` modules, then build thin admin routers (`require_admin_session` reads / `require_admin_role(ADMIN)` writes, `get_tenant_db`, PHI-free audit) that reuse those services and the existing repositories. No DB migration — every table already exists and is `TenantScoped`.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, Pydantic v2, pytest (testcontainers + real Alembic, app connects as non-superuser `usan_app` so RLS is live), ruff, mypy, uv.

## Global Constraints

- **Auth tiers:** router-level `dependencies=[Depends(require_admin_session)]`; per-mutation `_: AdminPrincipal = Depends(require_admin_role(AdminRole.ADMIN))`. Reads need VIEWER (login only); writes need ADMIN. A 403 fires before the handler (a write on a missing row 403s, not 404s).
- **Sessions/DB:** all new admin handlers use `db: AsyncSession = Depends(get_tenant_db)` (RLS-scoped to the principal's active org). Never `get_db`. Never accept the operator token.
- **Audit:** every write calls `admin_audit.record(db, actor_email=..., action=..., entity_type=..., entity_id=..., detail={...})` in the SAME transaction as the mutation, then one `await db.commit()`. `detail` carries UUIDs/flags/counts ONLY — never name, phone, or `dynamic_vars` values.
- **PHI:** phones render only via `mask_phone(...)` (`"***"+last4`, `"unknown"` if absent) in response bodies. Editing/removing a number means submitting a full replacement E.164; the stored full number is never echoed back.
- **Validators (reuse, do not re-invent):** `E164_PATTERN`, `PHONE_MAX_LENGTH=20`, `TIMEZONE_MAX_LENGTH=64`, `validate_iana_timezone`, `reject_nested_dynamic_vars` from `schemas/_validators.py`; `MAX_DYNAMIC_VARS_BYTES=8192` and `RESERVED_KEY_PREFIXES=("sched:","batch:")` from `schemas/call.py`.
- **Idempotency key for call-now:** mint a plain unique non-reserved value `f"admin-{uuid.uuid4()}"` (must be 1–255 chars and MUST NOT start with `sched:`/`batch:`). The call's `origin` reads `null` and falls in the calls-list `origin=adhoc` bucket.
- **Contact model quirk:** the JSONB metadata column is the Python attribute `meta` but the DB column `metadata`. `repositories.contacts.update_contact(db, id, fields)` enforces allow-list `{name, phone_e164, timezone, external_id, preferred_voice}` and special-cases the dict key `"metadata"` → `meta`; any other key raises `ValueError`. Uniqueness is composite `(phone_e164, organization_id)` and `(external_id, organization_id)` → 409 on violation.
- **Commands:** from `apps/api`: `uv run pytest -v` (run named tests with `-k`/path), `ruff check . && ruff format .`, `uv run mypy` (all three are CI gates — run before every commit).
- **Commit format:** `type(scope): description`, scope `api`. No attribution trailer (disabled globally).

---

### Task 1: Extract `services/outbound_calls.py` (shared call enqueue/dispatch core)

Behavior-preserving refactor: move the dispatch core and the profile-liveness guard out of `routers/calls.py` so the new admin call-now endpoint (Task 7) reuses identical logic. The existing operator call tests are the regression guard.

**Files:**
- Create: `apps/api/src/usan_api/services/__init__.py` (empty package marker — only if `services/` does not already exist)
- Create: `apps/api/src/usan_api/services/outbound_calls.py`
- Modify: `apps/api/src/usan_api/routers/calls.py` (remove the moved code; import from the service)

**Interfaces:**
- Produces:
  - `services.outbound_calls.require_live_override(db: AsyncSession, profile_id: uuid.UUID) -> None` (422 unless ACTIVE+published)
  - `services.outbound_calls.create_and_dispatch(db: AsyncSession, *, body: CreateCallRequest, contact: Contact, settings: Settings) -> CallResponse`
  - `services.outbound_calls.OVERRIDE_ERROR: str`
- Consumes (existing): `repositories.calls`, `repositories.wellness`, `repositories.family_tasks`, `repositories.medication_reminders`, `repositories.personal_facts`, `repositories.conversation_summaries`, `repositories.survey_results`, `repositories.agent_profiles`, `builtin_vars`, `livekit_dispatch`, `dialer`.

- [ ] **Step 1: Confirm whether `services/` exists**

Run: `ls apps/api/src/usan_api/services/ 2>/dev/null || echo "NO services dir"`
If "NO services dir", create `apps/api/src/usan_api/services/__init__.py` as an empty file. Otherwise skip creating the package marker.

- [ ] **Step 2: Run the existing operator call tests to capture the green baseline**

Run: `cd apps/api && uv run pytest tests/ -v -k "call" `
Expected: PASS (record the count; this same set must stay green after the move). If any are already failing pre-change, stop and report — do not refactor on a red baseline.

- [ ] **Step 3: Create `services/outbound_calls.py` by moving the code verbatim**

Move `_OVERRIDE_ERROR`, `_require_live_override`, and `_create_and_dispatch` out of `routers/calls.py` into the new module, renaming to public names and dropping the vestigial `response` parameter (the body never used it):

```python
"""Shared outbound-call enqueue/dispatch core.

Plane-agnostic: takes whatever AsyncSession the caller holds — the operator
``POST /v1/calls`` (get_db) and the admin ``POST /v1/admin/calls`` (get_tenant_db)
both delegate here, so DNC/liveness/dispatch/retry behavior is identical.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import dialer, livekit_dispatch
from usan_api.builtin_vars import build_memory_params, resolve_builtin_vars
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Contact
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import conversation_summaries as conversation_summaries_repo
from usan_api.repositories import family_tasks as family_tasks_repo
from usan_api.repositories import medication_reminders as medication_reminders_repo
from usan_api.repositories import personal_facts as personal_facts_repo
from usan_api.repositories import survey_results as survey_results_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.call import CallResponse, CreateCallRequest
from usan_api.settings import Settings

OVERRIDE_ERROR = "profile_override must reference an active profile with a published version"


async def require_live_override(db: AsyncSession, profile_id: uuid.UUID) -> None:
    """422 unless the override would actually take effect (ACTIVE + published)."""
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise HTTPException(status_code=422, detail=OVERRIDE_ERROR)


async def create_and_dispatch(
    db: AsyncSession,
    *,
    body: CreateCallRequest,
    contact: Contact,
    settings: Settings,
) -> CallResponse:
    """Persist a queued call, dispatch the agent, schedule the background dial."""
    room = f"usan-outbound-{uuid.uuid4()}"
    call = await calls_repo.create_call(
        db,
        contact_id=contact.id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        idempotency_key=body.idempotency_key,
        livekit_room=room,
        dynamic_vars=body.dynamic_vars,
        profile_override=body.profile_override,
    )
    await db.commit()

    last = await wellness_repo.get_latest_for_contact(db, contact.id)
    open_tasks = await family_tasks_repo.list_open_family_tasks(db, contact_id=contact.id)
    pending_meds = await medication_reminders_repo.list_pending(db, contact_id=contact.id)
    facts = await personal_facts_repo.list_active(db, contact_id=contact.id)
    summary = await conversation_summaries_repo.get_latest(db, contact_id=contact.id)
    memory = build_memory_params(
        facts, summary, timezone=contact.timezone or "", now=datetime.now(UTC)
    )
    period_month = survey_results_repo.month_anchor(contact.timezone or "", datetime.now(UTC))
    survey_due = not await survey_results_repo.exists_for_month(
        db, contact_id=contact.id, period_month=period_month
    )
    resolved_vars, timezone = resolve_builtin_vars(
        contact,
        last,
        direction="outbound",
        open_family_tasks=[t.message for t in open_tasks],
        pending_med_reasks=[r.medication_name for r in pending_meds],
        survey_due=survey_due,
        **memory,
    )
    try:
        await livekit_dispatch.dispatch_agent(
            call, settings=settings, resolved_vars=resolved_vars, timezone=timezone
        )
    except livekit_dispatch.OutboundDispatchError as exc:
        await calls_repo.set_status(db, call.id, CallStatus.FAILED, error={"reason": str(exc)})
        await db.commit()
        raise HTTPException(status_code=503, detail="outbound calling is not available") from exc
    except Exception as exc:
        await calls_repo.set_status(
            db,
            call.id,
            CallStatus.FAILED,
            error={"reason": "dispatch_error", "exc_type": type(exc).__name__},
        )
        await calls_repo.schedule_retry(db, call.id)
        await db.commit()
        logger.bind(call_id=str(call.id), err=type(exc).__name__).error("Agent dispatch failed")
        raise HTTPException(status_code=502, detail="failed to dispatch outbound call") from exc

    dialing = await calls_repo.set_status(db, call.id, CallStatus.DIALING)
    await db.commit()
    dialer.schedule_dial(call.id, settings)
    logger.bind(call_id=str(call.id), room=room).info("Outbound call dispatched; dialing")
    return CallResponse.from_model(dialing or call)
```

- [ ] **Step 3b: Rewire `routers/calls.py` to use the service**

In `routers/calls.py`: delete the now-moved `_OVERRIDE_ERROR`, `_require_live_override`, and `_create_and_dispatch` definitions. Add `from usan_api.services import outbound_calls`. Keep `_idempotent_replay` in the router (operator-only replay semantics). Update the two call sites inside `enqueue_call`:
  - replace `await _require_live_override(db, body.profile_override)` with `await outbound_calls.require_live_override(db, body.profile_override)`
  - replace `return await _create_and_dispatch(db, body, contact, settings, response)` with `return await outbound_calls.create_and_dispatch(db, body=body, contact=contact, settings=settings)`
Remove any imports in `calls.py` that are now unused ONLY if `ruff` flags them (e.g. `build_memory_params`, `resolve_builtin_vars`, repo imports used solely by the moved function). Leave imports still used by the remaining router code.

- [ ] **Step 4: Run the regression set + lint + types**

Run: `cd apps/api && uv run pytest tests/ -v -k "call"`
Expected: PASS, same count as Step 2.
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean (fix any unused-import / type errors surfaced by the move).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/services/ apps/api/src/usan_api/routers/calls.py
git commit -m "refactor(api): extract outbound-call enqueue/dispatch into services.outbound_calls"
```

---

### Task 2: Extract `services/schedules.py` (shared schedule validation/CRUD core)

Behavior-preserving refactor of `routers/schedules.py` so the admin schedule router (Task 6) reuses the window computation, slot-uniqueness, and merged-state revalidation. Existing schedule tests are the regression guard.

**Files:**
- Create: `apps/api/src/usan_api/services/schedules.py`
- Modify: `apps/api/src/usan_api/routers/schedules.py` (import from service; drop moved helpers)

**Interfaces:**
- Produces:
  - `services.schedules.compute_next_run_at(schedule_like: CallSchedule | CreateScheduleRequest, tz: str) -> datetime` (422 fail-closed)
  - `services.schedules.build_create(db, *, body: CreateScheduleRequest, contact: Contact) -> CallSchedule` (raises HTTPException 409 slot-exists / 422 window/liveness; adds+flushes, does NOT commit)
  - `services.schedules.apply_update(db, *, schedule: CallSchedule, body: UpdateScheduleRequest, contact: Contact) -> None` (mutates in place, recomputes `next_run_at`; does NOT commit)
- Consumes: `repositories.call_schedules`, `repositories.agent_profiles`, `schedule_windows.next_run_at`, `schedule_windows.days_to_mask`, `services.outbound_calls.require_live_override`.

- [ ] **Step 1: Capture green baseline**

Run: `cd apps/api && uv run pytest tests/test_schedules_api.py -v`
Expected: PASS (record count).

- [ ] **Step 2: Create `services/schedules.py`**

```python
"""Shared call-schedule validation + create/update core.

The operator ``/v1/schedules`` router and the admin ``/v1/admin/schedules`` router
both delegate here so window/quiet-hours/slot/override rules cannot drift between
the two planes. Callers own the surrounding transaction (commit + IntegrityError
handling) so the mutation and its audit row land in one commit.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallSchedule, Contact
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.schedule_windows import days_to_mask, next_run_at
from usan_api.schemas.schedule import CreateScheduleRequest, UpdateScheduleRequest
from usan_api.services.outbound_calls import require_live_override


def compute_next_run_at(schedule_like: CallSchedule | CreateScheduleRequest, tz: str) -> datetime:
    """next_run_at from now for the (merged) window/days; ValueError -> 422 fail-closed."""
    if isinstance(schedule_like, CreateScheduleRequest):
        days_mask = schedule_like.days_mask
    else:
        days_mask = schedule_like.days_of_week
    try:
        computed = next_run_at(
            datetime.now(UTC),
            tz,
            window_start=schedule_like.window_start_local,
            window_end=schedule_like.window_end_local,
            days_mask=days_mask,
        )
        if computed is None:
            raise ValueError("schedule window produced no dialable time")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return computed


async def build_create(
    db: AsyncSession, *, body: CreateScheduleRequest, contact: Contact
) -> CallSchedule:
    """Validate + insert (flush, no commit). Raises 409 on slot clash, 422 on window/override."""
    if (
        await schedules_repo.get_by_contact_slot(db, contact_id=body.contact_id, slot=body.slot)
        is not None
    ):
        raise HTTPException(status_code=409, detail=f"contact already has a {body.slot} schedule")
    if body.profile_override is not None:
        await require_live_override(db, body.profile_override)
    computed = compute_next_run_at(body, contact.timezone)
    return await schedules_repo.create_schedule(
        db,
        contact_id=body.contact_id,
        slot=body.slot,
        window_start_local=body.window_start_local,
        window_end_local=body.window_end_local,
        days_of_week=body.days_mask,
        enabled=body.enabled,
        dynamic_vars=body.dynamic_vars,
        profile_override=body.profile_override,
        next_run_at=computed,
    )


async def apply_update(
    db: AsyncSession,
    *,
    schedule: CallSchedule,
    body: UpdateScheduleRequest,
    contact: Contact,
) -> None:
    """Merge the PATCH body onto ``schedule`` in place + recompute next_run_at (no commit)."""
    if body.enabled is not None:
        schedule.enabled = body.enabled
    if body.window_start_local is not None and body.window_end_local is not None:
        schedule.window_start_local = body.window_start_local
        schedule.window_end_local = body.window_end_local
    if body.days_of_week is not None:
        schedule.days_of_week = days_to_mask(body.days_of_week)
    if body.dynamic_vars is not None:
        schedule.dynamic_vars = body.dynamic_vars
    if "profile_override" in body.model_fields_set:  # explicit null clears the override
        if body.profile_override is not None:
            await require_live_override(db, body.profile_override)
        schedule.profile_override = body.profile_override
    schedule.next_run_at = compute_next_run_at(schedule, contact.timezone)
```

- [ ] **Step 3: Rewire `routers/schedules.py`**

In `routers/schedules.py`: delete `_compute_next_run_at`, `_require_live_override`, and the body logic now in the service. Add `from usan_api.services import schedules as schedules_service`. Rewrite the handler bodies to delegate (auth/audit unchanged):
  - `create_schedule`: after the `contact` 404 check, replace the slot pre-check + liveness + compute + `create_schedule` block with:
    ```python
        try:
            schedule = await schedules_service.build_create(db, body=body, contact=contact)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409, detail=f"contact already has a {body.slot} schedule"
            ) from exc
    ```
  - `update_schedule`: replace the merge/recompute block with `await schedules_service.apply_update(db, schedule=schedule, body=body, contact=contact)` then keep the existing `await db.commit()` + `await db.refresh(schedule)` + `_audit(...)`.
  - Keep `_get_or_404`, `_audit`, list/get/delete handlers as-is.

- [ ] **Step 4: Regression + lint + types**

Run: `cd apps/api && uv run pytest tests/test_schedules_api.py -v`
Expected: PASS, same count as Step 1.
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/services/schedules.py apps/api/src/usan_api/routers/schedules.py
git commit -m "refactor(api): extract schedule validation/CRUD into services.schedules"
```

---

### Task 3: Repository additions — `dnc.list_entries` + `contacts.delete_contact`

**Files:**
- Modify: `apps/api/src/usan_api/repositories/dnc.py`
- Modify: `apps/api/src/usan_api/repositories/contacts.py`
- Test: `apps/api/tests/test_dnc_repo.py` (create), `apps/api/tests/test_contacts_repo.py` (create or append if it exists)

**Interfaces:**
- Produces:
  - `repositories.dnc.list_entries(db: AsyncSession, *, limit: int = 200, offset: int = 0) -> list[DNCEntry]`
  - `repositories.contacts.delete_contact(db: AsyncSession, contact_id: uuid.UUID) -> bool` (True if a row was deleted)

- [ ] **Step 1: Write failing repo tests**

Create `apps/api/tests/test_dnc_repo.py`:

```python
import uuid

from sqlalchemy import text

from usan_api.repositories import dnc as dnc_repo
from usan_api.tenant_context import set_tenant_context


async def _org(app_session) -> uuid.UUID:
    org_id = (
        await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))
    ).scalar_one()
    await set_tenant_context(app_session, org_id)
    return org_id


async def test_list_entries_returns_added(app_session):
    await _org(app_session)
    await dnc_repo.add_entry(app_session, "+15550000001", "requested")
    await dnc_repo.add_entry(app_session, "+15550000002", None)
    rows = await dnc_repo.list_entries(app_session, limit=50, offset=0)
    phones = {r.phone_e164 for r in rows}
    assert {"+15550000001", "+15550000002"} <= phones


async def test_list_entries_respects_limit(app_session):
    await _org(app_session)
    await dnc_repo.add_entry(app_session, "+15550000003", None)
    await dnc_repo.add_entry(app_session, "+15550000004", None)
    rows = await dnc_repo.list_entries(app_session, limit=1, offset=0)
    assert len(rows) == 1
```

Create `apps/api/tests/test_contacts_repo.py` (append the test if the file already exists):

```python
import uuid

from sqlalchemy import text

from usan_api.repositories import contacts as contacts_repo
from usan_api.tenant_context import set_tenant_context


async def test_delete_contact_removes_row(app_session):
    org_id = (
        await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))
    ).scalar_one()
    await set_tenant_context(app_session, org_id)
    c = await contacts_repo.create_contact(
        app_session, name="Del Me", phone_e164="+15550000010", timezone="America/Chicago"
    )
    assert await contacts_repo.delete_contact(app_session, c.id) is True
    assert await contacts_repo.get_contact(app_session, c.id) is None


async def test_delete_contact_missing_returns_false(app_session):
    org_id = (
        await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))
    ).scalar_one()
    await set_tenant_context(app_session, org_id)
    assert await contacts_repo.delete_contact(app_session, uuid.uuid4()) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest tests/test_dnc_repo.py tests/test_contacts_repo.py -v`
Expected: FAIL with `AttributeError: module 'usan_api.repositories.dnc' has no attribute 'list_entries'` (and same for `delete_contact`).

- [ ] **Step 3: Implement `dnc.list_entries`**

Append to `apps/api/src/usan_api/repositories/dnc.py` (it already imports `select` and `DNCEntry`):

```python
async def list_entries(db: AsyncSession, *, limit: int = 200, offset: int = 0) -> list[DNCEntry]:
    """Newest-first page of DNC entries for the current org (RLS-scoped)."""
    result = await db.execute(
        select(DNCEntry).order_by(DNCEntry.added_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())
```

- [ ] **Step 4: Implement `contacts.delete_contact`**

Append to `apps/api/src/usan_api/repositories/contacts.py` (use the model + a delete; mirror the module's existing session style). Add `from sqlalchemy import delete` to the imports if not present:

```python
async def delete_contact(db: AsyncSession, contact_id: uuid.UUID) -> bool:
    """Delete a contact by id; return True iff a row was removed. CASCADE drops the
    contact's schedules; calls.contact_id is ON DELETE SET NULL so history survives."""
    result = await db.execute(delete(Contact).where(Contact.id == contact_id))
    return result.rowcount > 0
```

- [ ] **Step 5: Run to verify pass**

Run: `cd apps/api && uv run pytest tests/test_dnc_repo.py tests/test_contacts_repo.py -v`
Expected: PASS.
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/repositories/dnc.py apps/api/src/usan_api/repositories/contacts.py apps/api/tests/test_dnc_repo.py apps/api/tests/test_contacts_repo.py
git commit -m "feat(api): add dnc.list_entries and contacts.delete_contact repository methods"
```

---

### Task 4: Schemas for admin call-now, contact create/edit, and DNC list

**Files:**
- Modify: `apps/api/src/usan_api/schemas/admin.py` (add contact create/update + detail)
- Modify: `apps/api/src/usan_api/schemas/admin_calls.py` (add `AdminCreateCallRequest`)
- Modify: `apps/api/src/usan_api/schemas/dnc.py` (add `AdminDNCResponse`)
- Test: `apps/api/tests/test_admin_orchestration_schemas.py` (create)

**Interfaces:**
- Produces:
  - `schemas.admin.ContactCreate` — `name: str`, `phone_e164: str (E164)`, `timezone: str (IANA)`, `external_id: str | None`, `preferred_voice: str | None`, `metadata: dict[str, Any] = {}`
  - `schemas.admin.ContactUpdate` — all-optional PATCH, `extra="forbid"`; same field set as create
  - `schemas.admin.ContactDetail` — `ContactSummary` fields + `external_id: str | None`, `preferred_voice: str | None`, `metadata: dict[str, Any]`, `created_at: datetime`, `updated_at: datetime`
  - `schemas.admin_calls.AdminCreateCallRequest` — `contact_id: uuid.UUID`, `dynamic_vars: dict[str, Any] = {}`, `profile_override: uuid.UUID | None = None`
  - `schemas.dnc.AdminDNCResponse` — `masked_phone: str`, `reason: str | None`, `added_at: datetime`, `from_model(entry)`

- [ ] **Step 1: Write failing schema tests**

Create `apps/api/tests/test_admin_orchestration_schemas.py`:

```python
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.schemas.admin import ContactCreate, ContactUpdate
from usan_api.schemas.admin_calls import AdminCreateCallRequest
from usan_api.schemas.dnc import AdminDNCResponse


def test_contact_create_rejects_bad_phone():
    with pytest.raises(ValidationError):
        ContactCreate(name="A", phone_e164="5551234", timezone="America/Chicago")


def test_contact_create_rejects_bad_timezone():
    with pytest.raises(ValidationError):
        ContactCreate(name="A", phone_e164="+15551230000", timezone="Mars/Phobos")


def test_contact_create_ok():
    c = ContactCreate(name="A", phone_e164="+15551230000", timezone="America/Chicago")
    assert c.metadata == {}
    assert c.external_id is None


def test_contact_update_forbids_unknown_field():
    with pytest.raises(ValidationError):
        ContactUpdate(agent_profile_id=str(uuid.uuid4()))  # not editable here


def test_admin_create_call_defaults():
    body = AdminCreateCallRequest(contact_id=uuid.uuid4())
    assert body.dynamic_vars == {}
    assert body.profile_override is None


def test_admin_dnc_response_masks():
    class _E:
        phone_e164 = "+15551239999"
        reason = "x"
        added_at = datetime(2026, 6, 23, tzinfo=UTC)

    out = AdminDNCResponse.from_model(_E())
    assert out.masked_phone.endswith("9999")
    assert "+1555" not in out.masked_phone
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest tests/test_admin_orchestration_schemas.py -v`
Expected: FAIL with ImportError (`ContactCreate` etc. not defined).

- [ ] **Step 3: Add contact schemas to `schemas/admin.py`**

`schemas/admin.py` already imports `Field`, `field_validator`, `TIMEZONE_MAX_LENGTH`, `validate_iana_timezone`. Add the E.164 + datetime imports as needed (`from usan_api.schemas._validators import E164_PATTERN, PHONE_MAX_LENGTH`, `from datetime import datetime`, `from typing import Any`, `from pydantic import ConfigDict`). Append:

```python
class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN)
    timezone: str = Field(min_length=1, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=200)
    preferred_voice: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)


class ContactUpdate(BaseModel):
    """All-optional PATCH; extra='forbid' so privileged/unknown keys 422 instead of
    silently no-opping. agent_profile_id/timezone keep their dedicated endpoints."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone_e164: str | None = Field(
        default=None, min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN
    )
    timezone: str | None = Field(default=None, min_length=1, max_length=TIMEZONE_MAX_LENGTH)
    external_id: str | None = Field(default=None, max_length=200)
    preferred_voice: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str | None) -> str | None:
        return None if v is None else validate_iana_timezone(v)


class ContactDetail(ContactSummary):
    external_id: str | None = None
    preferred_voice: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Add `AdminCreateCallRequest` to `schemas/admin_calls.py`**

Append (reuse the same dynamic_vars cap rule as `CreateCallRequest`):

```python
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from usan_api.schemas._validators import reject_nested_dynamic_vars
from usan_api.schemas.call import MAX_DYNAMIC_VARS_BYTES


class AdminCreateCallRequest(BaseModel):
    """Admin 'call now' body. No idempotency_key — the endpoint mints a unique
    non-reserved key server-side (origin=adhoc)."""

    contact_id: uuid.UUID
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)
    profile_override: uuid.UUID | None = None

    @field_validator("dynamic_vars")
    @classmethod
    def _cap_dynamic_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        reject_nested_dynamic_vars(v)
        if len(json.dumps(v)) > MAX_DYNAMIC_VARS_BYTES:
            raise ValueError(f"dynamic_vars must serialize to <= {MAX_DYNAMIC_VARS_BYTES} bytes")
        return v
```

(If `schemas/admin_calls.py` already imports some of these names, do not duplicate the imports.)

- [ ] **Step 5: Add `AdminDNCResponse` to `schemas/dnc.py`**

Append (import `mask_phone`):

```python
from usan_api.masking import mask_phone


class AdminDNCResponse(BaseModel):
    """Admin-plane DNC row — masked phone only (spec §6.3)."""

    masked_phone: str
    reason: str | None
    added_at: datetime

    @classmethod
    def from_model(cls, entry: DNCEntry) -> "AdminDNCResponse":
        return cls(
            masked_phone=mask_phone(entry.phone_e164), reason=entry.reason, added_at=entry.added_at
        )
```

- [ ] **Step 6: Run to verify pass + lint + types**

Run: `cd apps/api && uv run pytest tests/test_admin_orchestration_schemas.py -v`
Expected: PASS.
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/schemas/admin.py apps/api/src/usan_api/schemas/admin_calls.py apps/api/src/usan_api/schemas/dnc.py apps/api/tests/test_admin_orchestration_schemas.py
git commit -m "feat(api): add admin contact/call-now/dnc orchestration schemas"
```

---

### Task 5: Extend `routers/admin_contacts.py` — create / detail / update / delete

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_contacts.py`
- Test: `apps/api/tests/test_admin_contacts_crud_api.py` (create)

**Interfaces:**
- Consumes: `repositories.contacts` (`create_contact`, `get_contact`, `update_contact`, `delete_contact`), `schemas.admin` (`ContactCreate`, `ContactUpdate`, `ContactDetail`, `ContactSummary`), `admin_audit.record`, `mask_phone`, `get_tenant_db`, `require_admin_role(AdminRole.ADMIN)`, `get_actor_email`.

- [ ] **Step 1: Write failing endpoint tests**

Create `apps/api/tests/test_admin_contacts_crud_api.py`. Reuse the seeding/auth conventions from `tests/test_admin_contacts_api.py` (`admin_session` fixture puts the cookie on the jar; seed via the superuser engine where needed):

```python
import asyncio

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _viewer_cookie(client, async_database_url, email="viewer-crud@example.com"):
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, email, "viewer"))
    token = issue_session(
        email, active_org_id=None, role=AdminRole.VIEWER,
        is_super_admin=False, acting_as=False, settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)


def test_create_contact_then_detail(client, admin_session):
    r = client.post(
        "/v1/admin/contacts",
        json={"name": "Grace Hopper", "phone_e164": "+15551230101", "timezone": _TZ},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["masked_phone"].endswith("0101")

    detail = client.get(f"/v1/admin/contacts/{cid}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Grace Hopper"
    assert "phone_e164" not in detail.json()  # masked only, never raw


def test_create_duplicate_phone_409(client, admin_session):
    body = {"name": "Dup", "phone_e164": "+15551230202", "timezone": _TZ}
    assert client.post("/v1/admin/contacts", json=body).status_code == 201
    assert client.post("/v1/admin/contacts", json=body).status_code == 409


def test_patch_contact_name(client, admin_session):
    cid = client.post(
        "/v1/admin/contacts",
        json={"name": "Old", "phone_e164": "+15551230303", "timezone": _TZ},
    ).json()["id"]
    r = client.patch(f"/v1/admin/contacts/{cid}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"


def test_delete_contact(client, admin_session):
    cid = client.post(
        "/v1/admin/contacts",
        json={"name": "Bye", "phone_e164": "+15551230404", "timezone": _TZ},
    ).json()["id"]
    assert client.delete(f"/v1/admin/contacts/{cid}").status_code == 204
    assert client.get(f"/v1/admin/contacts/{cid}").status_code == 404


def test_viewer_cannot_create(client, async_database_url):
    _viewer_cookie(client, async_database_url)
    r = client.post(
        "/v1/admin/contacts",
        json={"name": "X", "phone_e164": "+15551230505", "timezone": _TZ},
    )
    assert r.status_code == 403


def test_create_requires_session(client):
    assert client.post(
        "/v1/admin/contacts",
        json={"name": "X", "phone_e164": "+15551230606", "timezone": _TZ},
    ).status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest tests/test_admin_contacts_crud_api.py -v`
Expected: FAIL — 405/404 on POST `/v1/admin/contacts` (route not defined) etc.

- [ ] **Step 3: Implement the new handlers in `routers/admin_contacts.py`**

Add imports: `from sqlalchemy.exc import IntegrityError` (already present), `from usan_api.schemas.admin import ContactCreate, ContactDetail, ContactUpdate`. Add a detail builder and the four handlers:

```python
def _detail(contact: Contact, profile_name: str | None) -> ContactDetail:
    return ContactDetail(
        id=contact.id,
        name=contact.name,
        masked_phone=mask_phone(contact.phone_e164),
        timezone=contact.timezone,
        agent_profile_id=contact.agent_profile_id,
        agent_profile_name=profile_name,
        external_id=contact.external_id,
        preferred_voice=contact.preferred_voice,
        metadata=contact.meta,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
    )


async def _profile_name(db: AsyncSession, contact: Contact) -> str | None:
    if contact.agent_profile_id is None:
        return None
    prof = await profiles_repo.get_profile(db, contact.agent_profile_id)
    return prof.name if prof else None


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ContactDetail)
async def create_contact(
    body: ContactCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactDetail:
    try:
        contact = await contacts_repo.create_contact(
            db,
            name=body.name,
            phone_e164=body.phone_e164,
            timezone=body.timezone,
            external_id=body.external_id,
            preferred_voice=body.preferred_voice,
            metadata=body.metadata,
        )
        await admin_audit.record(
            db,
            actor_email=actor,
            action="contact.create",
            entity_type="contact",
            entity_id=str(contact.id),
            detail={"has_external_id": body.external_id is not None},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="phone_e164 or external_id already in use"
        ) from exc
    return _detail(contact, await _profile_name(db, contact))


@router.get("/{contact_id}", response_model=ContactDetail)
async def get_contact_detail(
    contact_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> ContactDetail:
    contact = await contacts_repo.get_contact(db, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return _detail(contact, await _profile_name(db, contact))


@router.patch("/{contact_id}", response_model=ContactDetail)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ContactDetail:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    try:
        contact = await contacts_repo.update_contact(db, contact_id, fields)
        if contact is None:
            raise HTTPException(status_code=404, detail="contact not found")
        await admin_audit.record(
            db,
            actor_email=actor,
            action="contact.update",
            entity_type="contact",
            entity_id=str(contact_id),
            detail={"fields": sorted(fields.keys())},  # field NAMES only, never values
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="phone_e164 or external_id already in use"
        ) from exc
    await db.refresh(contact)
    return _detail(contact, await _profile_name(db, contact))


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    deleted = await contacts_repo.delete_contact(db, contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="contact not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="contact.delete",
        entity_type="contact",
        entity_id=str(contact_id),
    )
    await db.commit()
```

Note: `profiles_repo` is already imported as `from usan_api.repositories import agent_profiles as profiles_repo`; `uuid`, `status`, `HTTPException`, `Depends`, `Contact`, `mask_phone`, `admin_audit`, `get_tenant_db`, `require_admin_role`, `AdminRole`, `get_actor_email`, `contacts_repo` are all already imported in this router.

- [ ] **Step 4: Run to verify pass + lint + types**

Run: `cd apps/api && uv run pytest tests/test_admin_contacts_crud_api.py tests/test_admin_contacts_api.py -v`
Expected: PASS (new + pre-existing contact tests).
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_contacts.py apps/api/tests/test_admin_contacts_crud_api.py
git commit -m "feat(api): org-admin contact create/detail/update/delete endpoints"
```

---

### Task 6: New `routers/admin_schedules.py` — schedule CRUD + register

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_schedules.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router`)
- Test: `apps/api/tests/test_admin_schedules_api.py` (create)

**Interfaces:**
- Consumes: `services.schedules` (`build_create`, `apply_update`), `repositories.call_schedules` (`get_schedule`, `list_schedules`, `delete_schedule`), `repositories.contacts.get_contact`, `schemas.schedule` (`CreateScheduleRequest`, `UpdateScheduleRequest`, `ScheduleResponse`, `Slot`), `admin_audit.record`, `get_tenant_db`, `require_admin_role(AdminRole.ADMIN)`, `get_actor_email`.

- [ ] **Step 1: Write failing endpoint tests**

Create `apps/api/tests/test_admin_schedules_api.py`:

```python
import asyncio

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _make_contact(client, phone):
    return client.post(
        "/v1/admin/contacts",
        json={"name": "Sched Target", "phone_e164": phone, "timezone": _TZ},
    ).json()["id"]


def _create_body(cid):
    return {
        "contact_id": cid,
        "slot": "morning",
        "window_start_local": "09:30:00",
        "window_end_local": "11:00:00",
        "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        "enabled": True,
    }


def test_create_list_get_schedule(client, admin_session):
    cid = _make_contact(client, "+15551231001")
    r = client.post("/v1/admin/schedules", json=_create_body(cid))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["slot"] == "morning"

    listed = client.get("/v1/admin/schedules", params={"contact_id": cid}).json()
    assert any(s["id"] == sid for s in listed)
    assert client.get(f"/v1/admin/schedules/{sid}").status_code == 200


def test_duplicate_slot_409(client, admin_session):
    cid = _make_contact(client, "+15551231002")
    assert client.post("/v1/admin/schedules", json=_create_body(cid)).status_code == 201
    assert client.post("/v1/admin/schedules", json=_create_body(cid)).status_code == 409


def test_patch_disable_then_delete(client, admin_session):
    cid = _make_contact(client, "+15551231003")
    sid = client.post("/v1/admin/schedules", json=_create_body(cid)).json()["id"]
    r = client.patch(f"/v1/admin/schedules/{sid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.delete(f"/v1/admin/schedules/{sid}").status_code == 204
    assert client.get(f"/v1/admin/schedules/{sid}").status_code == 404


def test_viewer_cannot_create_schedule(client, admin_session, async_database_url):
    cid = _make_contact(client, "+15551231004")
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-sched@example.com", "viewer"))
    token = issue_session(
        "viewer-sched@example.com", active_org_id=None, role=AdminRole.VIEWER,
        is_super_admin=False, acting_as=False, settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.post("/v1/admin/schedules", json=_create_body(cid)).status_code == 403


def test_schedules_requires_session(client):
    assert client.get("/v1/admin/schedules").status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest tests/test_admin_schedules_api.py -v`
Expected: FAIL — 404 (router not registered).

- [ ] **Step 3: Create `routers/admin_schedules.py`**

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import CallSchedule
from usan_api.repositories import admin_audit
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.schemas.schedule import (
    CreateScheduleRequest,
    ScheduleResponse,
    Slot,
    UpdateScheduleRequest,
)
from usan_api.services import schedules as schedules_service

router = APIRouter(
    prefix="/v1/admin/schedules",
    tags=["admin-schedules"],
    dependencies=[Depends(require_admin_session)],
)


async def _get_or_404(db: AsyncSession, schedule_id: uuid.UUID) -> CallSchedule:
    schedule = await schedules_repo.get_schedule(db, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return schedule


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    contact_id: uuid.UUID | None = None,
    slot: Slot | None = None,
    last_result: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_tenant_db),
) -> list[ScheduleResponse]:
    rows = await schedules_repo.list_schedules(
        db, contact_id=contact_id, slot=slot, last_result=last_result, limit=limit, offset=offset
    )
    return [ScheduleResponse.from_model(s) for s in rows]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> ScheduleResponse:
    return ScheduleResponse.from_model(await _get_or_404(db, schedule_id))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScheduleResponse)
async def create_schedule(
    body: CreateScheduleRequest,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ScheduleResponse:
    contact = await contacts_repo.get_contact(db, body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    try:
        schedule = await schedules_service.build_create(db, body=body, contact=contact)
        await admin_audit.record(
            db,
            actor_email=actor,
            action="schedule.create",
            entity_type="schedule",
            entity_id=str(schedule.id),
            detail={"contact_id": str(body.contact_id), "slot": body.slot},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"contact already has a {body.slot} schedule"
        ) from exc
    return ScheduleResponse.from_model(schedule)


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: UpdateScheduleRequest,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ScheduleResponse:
    schedule = await _get_or_404(db, schedule_id)
    contact = await contacts_repo.get_contact(db, schedule.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    await schedules_service.apply_update(db, schedule=schedule, body=body, contact=contact)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="schedule.update",
        entity_type="schedule",
        entity_id=str(schedule_id),
        detail={"fields": sorted(body.model_dump(exclude_unset=True).keys())},
    )
    await db.commit()
    await db.refresh(schedule)
    return ScheduleResponse.from_model(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    schedule = await _get_or_404(db, schedule_id)
    await schedules_repo.delete_schedule(db, schedule)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="schedule.delete",
        entity_type="schedule",
        entity_id=str(schedule_id),
    )
    await db.commit()
```

- [ ] **Step 4: Register the router in `main.py`**

In `apps/api/src/usan_api/main.py`: add `admin_schedules` to the `routers` import line (the one importing the other `admin_*` modules), and add `app.include_router(admin_schedules.router)` to the `include_router` block (next to `admin_contacts`).

- [ ] **Step 5: Run to verify pass + lint + types**

Run: `cd apps/api && uv run pytest tests/test_admin_schedules_api.py -v`
Expected: PASS.
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_schedules.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_schedules_api.py
git commit -m "feat(api): org-admin schedule CRUD endpoints"
```

---

### Task 7: Extend `routers/admin_calls.py` — `POST /v1/admin/calls` ("call now")

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_calls.py`
- Test: `apps/api/tests/test_admin_call_now_api.py` (create)

**Interfaces:**
- Consumes: `services.outbound_calls` (`require_live_override`, `create_and_dispatch`), `repositories.contacts.get_contact`, `repositories.dnc` (`lock_phone`, `is_blocked`), `repositories.calls.create_call`, `schemas.admin_calls.AdminCreateCallRequest`, `schemas.call` (`CreateCallRequest`, `CallResponse`), `admin_audit.record`, `get_settings`, `get_actor_email`, `get_tenant_db`, `require_admin_role(AdminRole.ADMIN)`.

Behavior mirrors the operator `enqueue_call` minus idempotency replay: lock → liveness → DNC-block-or-dispatch. Mints `f"admin-{uuid.uuid4()}"` as the idempotency key.

- [ ] **Step 1: Write failing endpoint tests**

The dispatch path needs telephony; assert on the DNC-block branch (deterministic, no LiveKit) and the auth gates. Create `apps/api/tests/test_admin_call_now_api.py`:

```python
import asyncio
import uuid

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _contact(client, phone):
    return client.post(
        "/v1/admin/contacts",
        json={"name": "Call Target", "phone_e164": phone, "timezone": _TZ},
    ).json()["id"]


def test_call_now_dnc_blocked_returns_blocked(client, admin_session):
    phone = "+15551239001"
    cid = _contact(client, phone)
    # Put the number on the org's DNC list via the operator endpoint (same seeded org).
    assert client.post(
        "/v1/dnc", json={"phone_e164": phone, "reason": "test"},
        headers={"Authorization": "Bearer " + "o" * 32},
    ).status_code == 201

    r = client.post("/v1/admin/calls", json={"contact_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dnc_blocked"


def test_call_now_unknown_contact_404(client, admin_session):
    assert client.post(
        "/v1/admin/calls", json={"contact_id": str(uuid.uuid4())}
    ).status_code == 404


def test_call_now_viewer_403(client, admin_session, async_database_url):
    cid = _contact(client, "+15551239002")
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-call@example.com", "viewer"))
    token = issue_session(
        "viewer-call@example.com", active_org_id=None, role=AdminRole.VIEWER,
        is_super_admin=False, acting_as=False, settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.post("/v1/admin/calls", json={"contact_id": cid}).status_code == 403


def test_call_now_requires_session(client):
    assert client.post(
        "/v1/admin/calls", json={"contact_id": str(uuid.uuid4())}
    ).status_code == 401
```

Note: the DNC-block test depends on the operator `POST /v1/dnc` writing to the same org the admin session is scoped to. The `client` fixture scopes both planes to the seeded `usan` org, so this holds.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest tests/test_admin_call_now_api.py -v`
Expected: FAIL — 405/404 (route not defined).

- [ ] **Step 3: Implement `POST /v1/admin/calls`**

Add/extend imports at the top of `routers/admin_calls.py`: ensure `uuid`, `status`, `Depends`, `HTTPException`, `AsyncSession`, `Settings`, `get_settings` are imported (most already are for the read endpoints). Add: `from usan_api.auth import get_tenant_db, require_admin_role` (extend the existing `from usan_api.auth import ...` line, which already has `require_admin_session`), `from usan_api.admin_actor import get_actor_email`, `from usan_api.db.base import AdminRole, CallDirection, CallStatus` (extend the existing base import), `from usan_api.repositories import admin_audit`, `from usan_api.repositories import calls as calls_repo`, `from usan_api.repositories import contacts as contacts_repo`, `from usan_api.repositories import dnc as dnc_repo`, `from usan_api.schemas.admin_calls import AdminCreateCallRequest`, `from usan_api.schemas.call import CallResponse, CreateCallRequest`, `from usan_api.services import outbound_calls`. (Do not duplicate names already imported.) Append the handler:

```python
@router.post("/calls", status_code=status.HTTP_202_ACCEPTED, response_model=CallResponse)
async def call_now(
    body: AdminCreateCallRequest,
    db: AsyncSession = Depends(get_tenant_db),
    settings: Settings = Depends(get_settings),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> CallResponse:
    """Ad-hoc outbound call. DNC hard-blocks (200 dnc_blocked); quiet-hours are not
    enforced here (the UI carries the 'outside window' ack). Mints a unique
    non-reserved idempotency key so origin reads adhoc."""
    contact = await contacts_repo.get_contact(db, body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")

    # Build the internal create request with a server-minted adhoc key.
    create = CreateCallRequest(
        contact_id=body.contact_id,
        idempotency_key=f"admin-{uuid.uuid4()}",
        dynamic_vars=body.dynamic_vars,
        profile_override=body.profile_override,
    )

    await dnc_repo.lock_phone(db, contact.phone_e164)
    if create.profile_override is not None:
        await outbound_calls.require_live_override(db, create.profile_override)

    await admin_audit.record(
        db,
        actor_email=actor,
        action="call.enqueue",
        entity_type="contact",
        entity_id=str(contact.id),
        detail={
            "profile_override": str(create.profile_override) if create.profile_override else None
        },
    )

    if await dnc_repo.is_blocked(db, contact.phone_e164):
        call = await calls_repo.create_call(
            db,
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DNC_BLOCKED,
            idempotency_key=create.idempotency_key,
            dynamic_vars=create.dynamic_vars,
            profile_override=create.profile_override,
        )
        await db.commit()
        return CallResponse.from_model(call)

    # create_and_dispatch owns its own commits; commit the audit row first so it is
    # not lost if dispatch raises.
    await db.commit()
    return await outbound_calls.create_and_dispatch(
        db, body=create, contact=contact, settings=settings
    )
```

- [ ] **Step 4: Run to verify pass + lint + types**

Run: `cd apps/api && uv run pytest tests/test_admin_call_now_api.py tests/test_admin_calls_api.py -v`
Expected: PASS (new + pre-existing admin-calls read tests).
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_calls.py apps/api/tests/test_admin_call_now_api.py
git commit -m "feat(api): org-admin call-now endpoint (POST /v1/admin/calls)"
```

---

### Task 8: New `routers/admin_dnc.py` — DNC list/add/remove + register

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_dnc.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router`)
- Test: `apps/api/tests/test_admin_dnc_api.py` (create)

**Interfaces:**
- Consumes: `repositories.dnc` (`lock_phone`, `add_entry`, `remove_entry`, `list_entries`), `schemas.dnc` (`DNCCreate`, `AdminDNCResponse`), `schemas._validators` (`E164_PATTERN`, `PHONE_MAX_LENGTH`), `admin_audit.record`, `get_tenant_db`, `require_admin_role(AdminRole.ADMIN)`, `get_actor_email`.

- [ ] **Step 1: Write failing endpoint tests**

Create `apps/api/tests/test_admin_dnc_api.py`:

```python
import asyncio

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


def test_add_list_remove_dnc(client, admin_session):
    phone = "+15551240001"
    assert client.post(
        "/v1/admin/dnc", json={"phone_e164": phone, "reason": "req"}
    ).status_code == 201
    listed = client.get("/v1/admin/dnc").json()
    assert any(row["masked_phone"].endswith("0001") for row in listed)
    assert all("phone_e164" not in row for row in listed)  # masked only
    assert client.delete(f"/v1/admin/dnc/{phone}").status_code == 204


def test_remove_missing_404(client, admin_session):
    assert client.delete("/v1/admin/dnc/+15559990000").status_code == 404


def test_viewer_can_list_cannot_add(client, async_database_url):
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-dnc@example.com", "viewer"))
    token = issue_session(
        "viewer-dnc@example.com", active_org_id=None, role=AdminRole.VIEWER,
        is_super_admin=False, acting_as=False, settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/v1/admin/dnc").status_code == 200
    assert client.post("/v1/admin/dnc", json={"phone_e164": "+15551240002"}).status_code == 403


def test_dnc_requires_session(client):
    assert client.get("/v1/admin/dnc").status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && uv run pytest tests/test_admin_dnc_api.py -v`
Expected: FAIL — 404 (router not registered).

- [ ] **Step 3: Create `routers/admin_dnc.py`**

```python
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.repositories import admin_audit
from usan_api.repositories import dnc as dnc_repo
from usan_api.schemas._validators import E164_PATTERN, PHONE_MAX_LENGTH
from usan_api.schemas.dnc import AdminDNCResponse, DNCCreate

router = APIRouter(
    prefix="/v1/admin/dnc",
    tags=["admin-dnc"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("", response_model=list[AdminDNCResponse])
async def list_dnc(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[AdminDNCResponse]:
    rows = await dnc_repo.list_entries(db, limit=limit, offset=offset)
    return [AdminDNCResponse.from_model(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AdminDNCResponse)
async def add_dnc(
    body: DNCCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> AdminDNCResponse:
    await dnc_repo.lock_phone(db, body.phone_e164)
    entry = await dnc_repo.add_entry(db, body.phone_e164, body.reason)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="dnc.add",
        entity_type="dnc",
        entity_id=None,  # phone IS the PK and is PHI — never put it in entity_id/detail
        detail={"has_reason": body.reason is not None},
    )
    await db.commit()
    return AdminDNCResponse.from_model(entry)


@router.delete("/{phone_e164}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_dnc(
    phone_e164: str = Path(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    removed = await dnc_repo.remove_entry(db, phone_e164)
    if not removed:
        raise HTTPException(status_code=404, detail="not on DNC list")
    await admin_audit.record(
        db, actor_email=actor, action="dnc.remove", entity_type="dnc", entity_id=None
    )
    await db.commit()
```

- [ ] **Step 4: Register the router in `main.py`**

Add `admin_dnc` to the `admin_*` import line and `app.include_router(admin_dnc.router)` next to the other admin routers.

- [ ] **Step 5: Run to verify pass + full suite + lint + types**

Run: `cd apps/api && uv run pytest tests/test_admin_dnc_api.py -v`
Expected: PASS.
Run: `cd apps/api && uv run pytest -v`
Expected: PASS (whole suite — confirms no regression from any task).
Run: `cd apps/api && ruff check . && ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_dnc.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_dnc_api.py
git commit -m "feat(api): org-admin DNC list/add/remove endpoints"
```

---

### Task 9: RLS isolation proof for the new admin surface

A single cross-org test that proves the new endpoints leak nothing across tenants — the highest-value guarantee of the whole PR. Uses the `two_orgs` + per-principal `get_tenant_db` override pattern from `tests/test_rls_p2_isolation.py`.

**Files:**
- Test: `apps/api/tests/test_admin_orchestration_rls.py` (create)

- [ ] **Step 1: Write the isolation test**

Read `tests/test_rls_p2_isolation.py` first and reuse its `isolation_client` fixture and `_act_as_cookie` helper verbatim (do not re-invent the per-principal override). The test must:
  1. seed one contact into org A and one into org B via the **superuser** engine (set `organization_id` explicitly),
  2. act as an admin of org A, `GET /v1/admin/contacts` and assert A's id is present and B's is absent,
  3. attempt `GET /v1/admin/contacts/{org_b_contact_id}` as org A and assert **404** (RLS hides it; not 200, not 500),
  4. create a DNC entry as org A, act as org B, `GET /v1/admin/dnc` and assert it is absent.

```python
# Skeleton — fill the fixtures/helpers to match tests/test_rls_p2_isolation.py exactly.
def test_admin_contacts_isolated_between_orgs(isolation_client, two_orgs, async_database_url):
    org_a, org_b = two_orgs
    ca = _seed_contact_in_org(async_database_url, org_a, "+15551250001")
    cb = _seed_contact_in_org(async_database_url, org_b, "+15551250002")

    _act_as_cookie(isolation_client, org_a)
    ids = {c["id"] for c in isolation_client.get("/v1/admin/contacts").json()}
    assert ca in ids and cb not in ids
    assert isolation_client.get(f"/v1/admin/contacts/{cb}").status_code == 404
```

- [ ] **Step 2: Run to verify it passes (RLS already enforces — this proves it)**

Run: `cd apps/api && uv run pytest tests/test_admin_orchestration_rls.py -v`
Expected: PASS. If org B's row is visible to org A, RLS scoping is wrong in a handler (it used `get_db` instead of `get_tenant_db`) — fix the handler, not the test.

- [ ] **Step 3: Commit**

```bash
git add apps/api/tests/test_admin_orchestration_rls.py
git commit -m "test(api): cross-org RLS isolation for admin orchestration endpoints"
```

---

## Self-Review

**Spec coverage (each §4 endpoint → task):** Contacts POST/GET/PATCH/DELETE → Task 5 (+ repo delete Task 3, schemas Task 4). Schedules GET/POST/GET-one/PATCH/DELETE → Task 6 (+ service Task 2). Call-now POST → Task 7 (+ service Task 1, schema Task 4). DNC GET/POST/DELETE → Task 8 (+ repo `list_entries` Task 3, schema Task 4). RLS isolation (spec §7) → Task 9. Shared-service extraction (spec §3.2) → Tasks 1–2. Audit PHI-free (spec §5) → every write handler. No migration (spec §8) → confirmed: only repo reads/writes + endpoints, no DDL.

**Placeholder scan:** Task 9 Step 1 is intentionally a skeleton because its fixtures must match `test_rls_p2_isolation.py` verbatim (instruction: read and reuse, do not re-invent) — every other code step is complete. No "TBD"/"handle errors appropriately"/"similar to Task N".

**Type consistency:** `create_and_dispatch(db, *, body, contact, settings) -> CallResponse` used identically in Task 1 (operator rewire) and Task 7. `require_live_override(db, profile_id)` used in Tasks 1, 2, 7. `build_create`/`apply_update` signatures match between Task 2 (definition) and Task 6 (call sites). `ContactDetail` (Task 4) is the response model in Task 5. `AdminDNCResponse.from_model` (Task 4) used in Task 8. Repo additions `dnc.list_entries(*, limit, offset)` and `contacts.delete_contact(id) -> bool` (Task 3) match their call sites in Tasks 8 and 5.

## Open items (carry into PR B / confirm during execution)
- Contact `DELETE` is a **hard delete** (CASCADE drops schedules; calls SET NULL preserves history) — matches the model FKs. If product wants soft-delete/block-if-in-use instead, that is a schema change deferred out of this PR.
- DNC removal requires re-submitting the full E.164 (masked list + full-number delete), consistent with the masked-phone-on-edit decision.
