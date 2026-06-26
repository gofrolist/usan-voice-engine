"""admin_calls repository: the admin-plane calls list read model (spec §4.1)."""

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.db.models import Call, Contact

MAX_ADMIN_CALLS_LIMIT = 500


async def list_calls(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID | None = None,
    status: CallStatus | None = None,
    direction: CallDirection | None = None,
    origin: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Call, str | None, str | None]]:
    """Admin calls list read model: Call + contact name/phone via outerjoin (spec §4.1).

    `origin` translates to idempotency_key prefix predicates over the reserved
    sched:/batch: namespace (A1); 'adhoc' is direction='outbound' AND (key IS NULL OR
    neither prefix) — the direction guard keeps inbound NULL-key calls out of Ad hoc.
    Documented caveats (spec §4.1): retry children carry no key (match adhoc, response
    origin null — the chain root carries provenance); pre-A1 squatted prefixes ~0.
    Ordered (created_at DESC, id DESC) — served exactly by idx_calls_created.
    `limit` clamped silently to 1..MAX_ADMIN_CALLS_LIMIT; `created_to` is EXCLUSIVE.
    """
    limit = max(1, min(limit, MAX_ADMIN_CALLS_LIMIT))
    offset = max(0, offset)
    # Web calls are a different modality (contactless browser sessions) and are
    # excluded from the contact/phone admin plane. direction is an internal
    # placeholder for web calls (call_type is authoritative) — excluding by
    # call_type prevents them from appearing as spurious inbound rows here and
    # in the direction-keyed CALLS_TOTAL metric (see routers/tools.py end_call).
    stmt = (
        select(Call, Contact.name, Contact.phone_e164)
        .outerjoin(Contact, Call.contact_id == Contact.id)
        .where(Call.call_type == CallType.PHONE_CALL)
    )
    if contact_id is not None:
        stmt = stmt.where(Call.contact_id == contact_id)
    if status is not None:
        stmt = stmt.where(Call.status == status)
    if direction is not None:
        stmt = stmt.where(Call.direction == direction)
    if created_from is not None:
        stmt = stmt.where(Call.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Call.created_at < created_to)
    if origin == "schedule":
        stmt = stmt.where(Call.idempotency_key.like("sched:%"))
    elif origin == "batch":
        stmt = stmt.where(Call.idempotency_key.like("batch:%"))
    elif origin == "adhoc":
        stmt = stmt.where(
            Call.direction == CallDirection.OUTBOUND,
            or_(
                Call.idempotency_key.is_(None),
                and_(
                    Call.idempotency_key.not_like("sched:%"),
                    Call.idempotency_key.not_like("batch:%"),
                ),
            ),
        )
    stmt = stmt.order_by(Call.created_at.desc(), Call.id.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]
