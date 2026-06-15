"""Operator admin plane for family contacts & tasks (US2 / T032).

Mirrors the existing ``admin_*`` routers: ``require_admin_session`` gates the whole
router (reads need a logged-in admin/viewer), mutations additionally require the ADMIN
role, and every mutation is audit-logged with NO PHI in the detail (variable/field NAMES
and UUIDs only — never the contact phone or task text). Contracts: contracts/admin-api.md.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import notifications
from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.repositories import family_reports as family_reports_repo
from usan_api.repositories import family_tasks as family_tasks_repo
from usan_api.repositories import sms_messages as sms_repo
from usan_api.schemas.family import (
    FamilyContactCreate,
    FamilyContactOut,
    FamilyContactUpdate,
    FamilyReportOut,
    FamilyTaskOut,
    FamilyTaskPatch,
)

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-family"],
    dependencies=[Depends(require_admin_session)],
)


# --- family contacts -------------------------------------------------------


@router.post("/family-contacts", response_model=FamilyContactOut, status_code=201)
async def create_family_contact(
    body: FamilyContactCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> FamilyContactOut:
    # Pre-check the FK so a bad elder_id is a clean 404, not a 500 on commit.
    if await elders_repo.get_elder(db, body.elder_id) is None:
        raise HTTPException(status_code=404, detail="elder not found")
    row = await family_contacts_repo.create_family_contact(
        db,
        elder_id=body.elder_id,
        name=body.name,
        phone_e164=body.phone_e164,
        relationship=body.relationship,
        alert_prefs=dict(body.alert_prefs),
    )
    await admin_audit.record(
        db,
        actor_email=actor,
        action="family_contact.create",
        entity_type="family_contact",
        entity_id=str(row.id),
        detail={"elder_id": str(body.elder_id)},  # no name/phone (PII)
    )
    await db.commit()
    return FamilyContactOut.model_validate(row)


@router.get("/family-contacts", response_model=list[FamilyContactOut])
async def list_family_contacts(
    elder_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> list[FamilyContactOut]:
    rows = await family_contacts_repo.list_family_contacts(db, elder_id=elder_id)
    return [FamilyContactOut.model_validate(r) for r in rows]


@router.patch("/family-contacts/{contact_id}", response_model=FamilyContactOut)
async def update_family_contact(
    contact_id: uuid.UUID,
    body: FamilyContactUpdate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> FamilyContactOut:
    fields = body.model_dump(exclude_unset=True)
    row = await family_contacts_repo.update_family_contact(db, contact_id, **fields)
    if row is None:
        raise HTTPException(status_code=404, detail="family contact not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="family_contact.update",
        entity_type="family_contact",
        entity_id=str(contact_id),
        detail={"fields": sorted(fields)},  # field NAMES only, never values (PII)
    )
    await db.commit()
    return FamilyContactOut.model_validate(row)


@router.delete("/family-contacts/{contact_id}", status_code=204)
async def delete_family_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> Response:
    # Read first to capture elder_id for the audit row before the contact is gone.
    existing = await family_contacts_repo.get_family_contact(db, contact_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="family contact not found")
    elder_id = existing.elder_id
    try:
        await family_contacts_repo.delete_family_contact(db, contact_id)
    except IntegrityError as exc:
        # A contact still referenced by family_tasks.family_contact_id (no ON DELETE).
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="contact still referenced by family tasks"
        ) from exc
    await admin_audit.record(
        db,
        actor_email=actor,
        action="family_contact.delete",
        entity_type="family_contact",
        entity_id=str(contact_id),
        detail={"elder_id": str(elder_id)},
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- family tasks ----------------------------------------------------------


@router.get("/family-tasks", response_model=list[FamilyTaskOut])
async def list_family_tasks(
    elder_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[FamilyTaskOut]:
    rows = await family_tasks_repo.list_family_tasks(
        db, elder_id=elder_id, status=status, limit=limit, offset=offset
    )
    return [FamilyTaskOut.model_validate(r) for r in rows]


@router.patch("/family-tasks/{task_id}", response_model=FamilyTaskOut)
async def patch_family_task(
    task_id: int,
    body: FamilyTaskPatch,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> FamilyTaskOut:
    if body.status == "closed":
        row = await family_tasks_repo.close_family_task(db, task_id, actor=actor)
    else:  # "open": approve a held (needs_safety_review) task so it can be conveyed
        row = await family_tasks_repo.approve_family_task(db, task_id, actor=actor)
    if row is None:
        # Disambiguate unknown id (404) from an illegal transition (409).
        if await family_tasks_repo.get_family_task(db, task_id) is None:
            raise HTTPException(status_code=404, detail="family task not found")
        raise HTTPException(status_code=409, detail="task not in a state for this transition")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="family_task.patch",
        entity_type="family_task",
        entity_id=str(task_id),
        detail={"status": body.status},  # no message text (PHI)
    )
    await db.commit()
    return FamilyTaskOut.model_validate(row)


# --- family reports (US8 / T079) -------------------------------------------


@router.get("/family-reports", response_model=list[FamilyReportOut])
async def list_family_reports(
    elder_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[FamilyReportOut]:
    # Session-gated read; the rich trend detail is the operator plane's (BAA) job. The
    # family SMS stays PHI-minimized — that is enforced at send time, not here.
    rows = await family_reports_repo.list_reports(db, elder_id=elder_id, limit=limit, offset=offset)
    return [
        FamilyReportOut.model_validate(report).model_copy(update={"elder_name": name})
        for report, name in rows
    ]


@router.post("/family-reports/{report_id}/resend", response_model=FamilyReportOut)
async def resend_family_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> FamilyReportOut:
    # Operator re-delivery of the monthly report SMS (e.g. the family says it never arrived).
    # Re-enqueues the SAME PHI-free body to the elder's opted-in family contacts with no
    # dedupe key, so each resend is a fresh outbox row. A report with no family contact
    # (status 'no_contact') has nobody to resend to -> 409.
    report = await family_reports_repo.get_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="family report not found")
    recipients = await family_contacts_repo.list_alert_recipients(
        db, elder_id=report.elder_id, kind="report"
    )
    if not recipients:
        raise HTTPException(status_code=409, detail="no family contact to resend to")
    body = notifications.build_family_report_body()
    for contact in recipients:
        await sms_repo.create_notification(
            db,
            elder_id=report.elder_id,
            to_number=contact.phone_e164,
            kind="family_report",
            body=body,
        )
    await admin_audit.record(
        db,
        actor_email=actor,
        action="family_report.resend",
        entity_type="family_report",
        entity_id=str(report_id),
        detail={"recipients": len(recipients)},  # count only; no phone/name (PII)
    )
    await db.commit()
    elder = await elders_repo.get_elder(db, report.elder_id)
    return FamilyReportOut.model_validate(report).model_copy(
        update={"elder_name": elder.name if elder is not None else None}
    )
