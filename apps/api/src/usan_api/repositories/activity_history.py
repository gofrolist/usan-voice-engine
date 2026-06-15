"""activity_history repository (US6 / T060).

Per-contact log of which mood-boosting activity was used when. ``list_recent`` returns the
contact's history MOST-RECENT-FIRST for ``activities_catalog.select_activity`` to pick a
non-recently-used entry; ``record_use`` appends the chosen one. The catalog itself is code
(``activities_catalog.py``) — this table only stores ``activity_key`` + ``used_at``. All
functions are flush-only; the caller commits.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ActivityHistory

# Bound the history scan: more than enough to cover the 30-day + last-3 recency window and
# the per-key least-recently-used fallback, while never loading an unbounded backlog. A use
# older than this window reads as "least-recently-used", which is the correct selection
# anyway, so the cap cannot pick a *recently*-used activity.
_RECENT_SCAN_LIMIT = 200


async def list_recent(
    db: AsyncSession, *, contact_id: uuid.UUID, limit: int = _RECENT_SCAN_LIMIT
) -> list[ActivityHistory]:
    """The contact's activity uses, most-recent-first (bounded)."""
    stmt = (
        select(ActivityHistory)
        .where(ActivityHistory.contact_id == contact_id)
        .order_by(ActivityHistory.used_at.desc(), ActivityHistory.id.desc())
        .limit(max(1, limit))
    )
    return list((await db.execute(stmt)).scalars().all())


async def record_use(
    db: AsyncSession, *, contact_id: uuid.UUID, activity_key: str, call_id: uuid.UUID
) -> ActivityHistory:
    """Append a use of ``activity_key`` for this contact/call. Flush-only."""
    row = ActivityHistory(contact_id=contact_id, activity_key=activity_key, call_id=call_id)
    db.add(row)
    await db.flush()
    return row
