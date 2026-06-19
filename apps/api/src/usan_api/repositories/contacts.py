import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import AgentProfile, Contact

# Columns an operator PUT /v1/contacts/{id} may set, mapped 1:1 from ContactUpdate's
# fields. The repo enforces this allow-list itself (not just the schema) so a privileged
# column — org_id, agent_profile_id, id, created_at — can never be written through the
# generic update path even if a future caller passes it (mass-assignment guard).
_UPDATABLE_CONTACT_FIELDS = frozenset(
    {"name", "phone_e164", "timezone", "external_id", "preferred_voice"}
)


async def create_contact(
    db: AsyncSession,
    *,
    name: str,
    phone_e164: str,
    timezone: str,
    external_id: str | None = None,
    preferred_voice: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Contact:
    contact = Contact(
        name=name,
        phone_e164=phone_e164,
        timezone=timezone,
        external_id=external_id,
        preferred_voice=preferred_voice,
        meta=metadata or {},
    )
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    return contact


async def get_contact(db: AsyncSession, contact_id: uuid.UUID) -> Contact | None:
    return await db.get(Contact, contact_id)


async def update_contact(
    db: AsyncSession, contact_id: uuid.UUID, fields: dict[str, Any]
) -> Contact | None:
    contact = await db.get(Contact, contact_id)
    if contact is None:
        return None
    for key, value in fields.items():
        if key == "metadata":
            contact.meta = value
        elif key in _UPDATABLE_CONTACT_FIELDS:
            setattr(contact, key, value)
        else:
            # ContactUpdate forbids extras (422), so this only fires for a direct repo
            # caller passing an unexpected key — fail loudly rather than blind-setattr an
            # arbitrary ORM attribute.
            raise ValueError(f"unexpected contact field: {key!r}")
    await db.flush()
    await db.refresh(contact)
    return contact


async def get_contact_by_phone(db: AsyncSession, phone_e164: str) -> Contact | None:
    """Look up an contact by E.164 phone (UNIQUE) — the inbound caller-ID lookup."""
    result = await db.execute(select(Contact).where(Contact.phone_e164 == phone_e164))
    return result.scalar_one_or_none()


async def list_with_profile(
    db: AsyncSession, *, limit: int = 200, offset: int = 0
) -> list[tuple[Contact, str | None]]:
    """Contacts with their assigned profile name (None if unassigned), ordered by name.

    Bounded by limit/offset: the contacts table is the full patient roster (potentially
    thousands), so the admin list pages through it rather than selecting every row.
    """
    result = await db.execute(
        select(Contact, AgentProfile.name)
        .outerjoin(AgentProfile, Contact.agent_profile_id == AgentProfile.id)
        .order_by(Contact.name)
        .limit(limit)
        .offset(offset)
    )
    return [(row[0], row[1]) for row in result.all()]


async def assign_profile(
    db: AsyncSession, contact_id: uuid.UUID, profile_id: uuid.UUID | None
) -> Contact | None:
    """Set (or clear, with None) an contact's agent_profile_id. Caller commits."""
    contact = await db.get(Contact, contact_id)
    if contact is None:
        return None
    contact.agent_profile_id = profile_id
    await db.flush()
    return contact


async def set_timezone(db: AsyncSession, contact_id: uuid.UUID, timezone: str) -> Contact | None:
    """Set a contact's IANA timezone. Returns None if the contact is gone. Caller commits."""
    contact = await db.get(Contact, contact_id)
    if contact is None:
        return None
    contact.timezone = timezone
    await db.flush()
    return contact
