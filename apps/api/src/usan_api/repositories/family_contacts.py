"""family_contacts repository (US2 / T026).

Contacts are pre-registered per contact by an operator; the inbound Telnyx webhook
routes a sender by phone (``find_contacts_by_phone``) and the notification layer picks
alert recipients honoring each contact's ``alert_prefs`` (``list_alert_recipients``).
All functions are flush-only; the caller commits.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import FamilyContact

_MUTABLE_FIELDS = frozenset({"name", "phone_e164", "relationship", "alert_prefs"})


async def create_family_contact(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID,
    name: str,
    phone_e164: str,
    relationship: str | None = None,
    alert_prefs: dict[str, Any] | None = None,
) -> FamilyContact:
    row = FamilyContact(
        contact_id=contact_id,
        name=name,
        phone_e164=phone_e164,
        relationship=relationship,
        alert_prefs=alert_prefs or {},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_family_contact(db: AsyncSession, contact_id: uuid.UUID) -> FamilyContact | None:
    stmt = (
        select(FamilyContact)
        .where(FamilyContact.id == contact_id)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_family_contacts(db: AsyncSession, *, contact_id: uuid.UUID) -> list[FamilyContact]:
    stmt = (
        select(FamilyContact)
        .where(FamilyContact.contact_id == contact_id)
        .order_by(FamilyContact.created_at, FamilyContact.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def find_contacts_by_phone(db: AsyncSession, phone_e164: str) -> list[FamilyContact]:
    """All contacts with this number (may span >1 contact — phone is not globally unique)."""
    stmt = (
        select(FamilyContact)
        .where(FamilyContact.phone_e164 == phone_e164)
        .order_by(FamilyContact.created_at, FamilyContact.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_alert_recipients(
    db: AsyncSession, *, contact_id: uuid.UUID, kind: str
) -> list[FamilyContact]:
    """Contacts who should receive a ``kind`` alert for this contact.

    Fail-open: a contact receives the alert unless ``alert_prefs[kind]`` is explicitly
    falsey — a missing pref means opted-in, which is the safe default for life-safety
    alerts (FR-007/FR-009).
    """
    contacts = await list_family_contacts(db, contact_id=contact_id)
    return [c for c in contacts if c.alert_prefs.get(kind, True)]


async def update_family_contact(
    db: AsyncSession, contact_id: uuid.UUID, **fields: Any
) -> FamilyContact | None:
    """Patch the allowed mutable fields; unknown keys are ignored. Flush-only."""
    values = {k: v for k, v in fields.items() if k in _MUTABLE_FIELDS}
    if not values:
        return await get_family_contact(db, contact_id)
    stmt = (
        update(FamilyContact)
        .where(FamilyContact.id == contact_id)
        .values(**values)
        .returning(FamilyContact)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def delete_family_contact(db: AsyncSession, contact_id: uuid.UUID) -> bool:
    row = await get_family_contact(db, contact_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True
