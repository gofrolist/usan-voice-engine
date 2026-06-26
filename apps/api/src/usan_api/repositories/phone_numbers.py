import uuid
from typing import Any, cast

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import PhoneNumber

# Column allow-list for update_by_e164 (mass-assignment guard): id, organization_id,
# phone_e164, phone_number_type, created_at/updated_at are never writable via update.
_UPDATABLE_FIELDS = frozenset(
    {
        "nickname",
        "inbound_webhook_url",
        "inbound_sms_webhook_url",
        "allowed_inbound_country_list",
        "allowed_outbound_country_list",
        "fallback_number",
        "transport",
        "termination_uri",
        "sip_auth_username",
        "sip_auth_password",
        "inbound_agents",
        "outbound_agents",
        "inbound_sms_agents",
        "outbound_sms_agents",
    }
)


async def create_phone_number(
    db: AsyncSession,
    *,
    phone_e164: str,
    phone_number_type: str,
    nickname: str | None = None,
    area_code: int | None = None,
    inbound_webhook_url: str | None = None,
    inbound_sms_webhook_url: str | None = None,
    allowed_inbound_country_list: list[str] | None = None,
    allowed_outbound_country_list: list[str] | None = None,
    fallback_number: str | None = None,
    transport: str | None = None,
    termination_uri: str | None = None,
    sip_auth_username: str | None = None,
    sip_auth_password: str | None = None,
    inbound_agents: list[dict[str, Any]] | None = None,
    outbound_agents: list[dict[str, Any]] | None = None,
    inbound_sms_agents: list[dict[str, Any]] | None = None,
    outbound_sms_agents: list[dict[str, Any]] | None = None,
) -> PhoneNumber:
    pn = PhoneNumber(
        phone_e164=phone_e164,
        phone_number_type=phone_number_type,
        nickname=nickname,
        area_code=area_code,
        inbound_webhook_url=inbound_webhook_url,
        inbound_sms_webhook_url=inbound_sms_webhook_url,
        allowed_inbound_country_list=allowed_inbound_country_list,
        allowed_outbound_country_list=allowed_outbound_country_list,
        fallback_number=fallback_number,
        transport=transport,
        termination_uri=termination_uri,
        sip_auth_username=sip_auth_username,
        sip_auth_password=sip_auth_password,
        inbound_agents=inbound_agents,
        outbound_agents=outbound_agents,
        inbound_sms_agents=inbound_sms_agents,
        outbound_sms_agents=outbound_sms_agents,
    )
    db.add(pn)
    await db.flush()
    await db.refresh(pn)
    return pn


async def get_by_e164(db: AsyncSession, phone_e164: str) -> PhoneNumber | None:
    result = await db.execute(select(PhoneNumber).where(PhoneNumber.phone_e164 == phone_e164))
    return result.scalar_one_or_none()


async def update_by_e164(
    db: AsyncSession, phone_e164: str, fields: dict[str, Any]
) -> PhoneNumber | None:
    pn = await get_by_e164(db, phone_e164)
    if pn is None:
        return None
    for key, value in fields.items():
        if key in _UPDATABLE_FIELDS:
            setattr(pn, key, value)
        else:
            raise ValueError(f"unexpected phone_number field: {key!r}")
    await db.flush()
    await db.refresh(pn)
    return pn


async def delete_by_e164(db: AsyncSession, phone_e164: str) -> bool:
    result = cast(
        "CursorResult[Any]",
        await db.execute(delete(PhoneNumber).where(PhoneNumber.phone_e164 == phone_e164)),
    )
    return result.rowcount > 0


async def list_phone_numbers(
    db: AsyncSession, *, limit: int, descending: bool, after_id: uuid.UUID | None
) -> list[PhoneNumber]:
    """Keyset-paginate the org's numbers over (created_at, id). RLS scopes to the caller's org."""
    stmt = select(PhoneNumber)
    if after_id is not None:
        cursor = await db.get(PhoneNumber, after_id)
        if cursor is not None:
            if descending:
                stmt = stmt.where(
                    or_(
                        PhoneNumber.created_at < cursor.created_at,
                        and_(
                            PhoneNumber.created_at == cursor.created_at,
                            PhoneNumber.id < cursor.id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        PhoneNumber.created_at > cursor.created_at,
                        and_(
                            PhoneNumber.created_at == cursor.created_at,
                            PhoneNumber.id > cursor.id,
                        ),
                    )
                )
    if descending:
        stmt = stmt.order_by(PhoneNumber.created_at.desc(), PhoneNumber.id.desc())
    else:
        stmt = stmt.order_by(PhoneNumber.created_at.asc(), PhoneNumber.id.asc())
    stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())
