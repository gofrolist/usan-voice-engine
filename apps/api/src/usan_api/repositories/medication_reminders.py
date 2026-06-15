"""medication_reminders repository (US3 / T038).

State machine (guarded UPDATEs — the WHERE clause IS the state machine, like
follow_up_flags.update_status): not-taken → ``pending`` (attempt_count=0); each repeated
not-taken increments; reaching the cap → ``capped``; confirmation → ``cleared``. Only
``pending`` rows are surfaced as the ``pending_med_reasks`` builtin. The router turns a
just-``capped`` reminder into a routine ``follow_up_flags`` row so Clara stops nagging.
All functions are flush-only; the caller commits.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from usan_api.db.models import MedicationReminder

# A not-taken report opens the reminder at attempt_count=0; each *subsequent* not-taken
# report increments it. When attempt_count reaches MAX_REASK_ATTEMPTS the reminder is
# capped (→ a routine follow-up flag) and no longer surfaced as a re-ask, so Clara does
# not nag forever. So the cap fires on the (MAX_REASK_ATTEMPTS + 1)-th not-taken report
# (1 open + MAX_REASK_ATTEMPTS increments).
MAX_REASK_ATTEMPTS = 3

# Bound the per-contact pending injection so a backlog never floods the prompt.
_MAX_PENDING_INJECT = 20


async def get_reminder(db: AsyncSession, reminder_id: int) -> MedicationReminder | None:
    stmt = (
        select(MedicationReminder)
        .where(MedicationReminder.id == reminder_id)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_pending(
    db: AsyncSession, *, contact_id: uuid.UUID, medication_name: str
) -> MedicationReminder | None:
    """The single pending re-ask for this (contact, medication), if any."""
    stmt = (
        select(MedicationReminder)
        .where(
            MedicationReminder.contact_id == contact_id,
            MedicationReminder.medication_name == medication_name,
            MedicationReminder.status == "pending",
        )
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_pending(
    db: AsyncSession, *, contact_id: uuid.UUID, limit: int = _MAX_PENDING_INJECT
) -> list[MedicationReminder]:
    """Pending re-asks for an contact (oldest first) — the source of pending_med_reasks."""
    stmt = (
        select(MedicationReminder)
        .where(
            MedicationReminder.contact_id == contact_id,
            MedicationReminder.status == "pending",
        )
        .order_by(MedicationReminder.created_at, MedicationReminder.id)
        .limit(max(1, limit))
    )
    return list((await db.execute(stmt)).scalars().all())


async def open_or_refresh(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID,
    medication_name: str,
    call_id: uuid.UUID,
    max_attempts: int = MAX_REASK_ATTEMPTS,
) -> tuple[MedicationReminder, bool]:
    """Record a not-taken report. Returns ``(reminder, just_capped)``.

    No pending row → INSERT one (attempt_count=0, stamping opened_call_id). An existing
    pending row → increment attempt_count; when it reaches ``max_attempts`` the row
    transitions to ``capped`` and ``just_capped`` is True (the caller opens a routine
    follow-up flag exactly once). Idempotent against the rare concurrent-open race via the
    partial-unique ON CONFLICT. Flush-only.
    """
    existing = await get_pending(db, contact_id=contact_id, medication_name=medication_name)
    if existing is None:
        insert_stmt = (
            pg_insert(MedicationReminder)
            .values(
                contact_id=contact_id,
                medication_name=medication_name,
                status="pending",
                attempt_count=0,
                opened_call_id=call_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    MedicationReminder.contact_id,
                    MedicationReminder.medication_name,
                ],
                index_where=MedicationReminder.status == "pending",
            )
            .returning(MedicationReminder.id)
        )
        new_id = (await db.execute(insert_stmt)).scalar_one_or_none()
        if new_id is not None:
            await db.flush()
            created = await get_reminder(db, new_id)
            assert created is not None  # just inserted in this txn
            return created, False
        # Conflict (rare race): a concurrent not-taken report opened it — refresh that row.
        existing = await get_pending(db, contact_id=contact_id, medication_name=medication_name)
    if existing is None:  # pragma: no cover - the conflict guarantees a pending row exists
        raise RuntimeError("medication reminder conflict without a pending row")

    new_count = existing.attempt_count + 1
    new_status = "capped" if new_count >= max_attempts else "pending"
    updated = (
        await db.execute(
            update(MedicationReminder)
            .where(MedicationReminder.id == existing.id, MedicationReminder.status == "pending")
            .values(attempt_count=new_count, status=new_status, updated_at=func.now())
            .returning(MedicationReminder)
        )
    ).scalar_one_or_none()
    if updated is None:  # pragma: no cover - the pending row changed status mid-flight
        return existing, False
    return updated, updated.status == "capped"


async def clear_pending(
    db: AsyncSession, *, contact_id: uuid.UUID, medication_name: str, call_id: uuid.UUID
) -> MedicationReminder | None:
    """Confirmation: pending → cleared, recording which call confirmed it. Zero rows → None."""
    stmt = (
        update(MedicationReminder)
        .where(
            MedicationReminder.contact_id == contact_id,
            MedicationReminder.medication_name == medication_name,
            MedicationReminder.status == "pending",
        )
        .values(status="cleared", cleared_call_id=call_id, updated_at=func.now())
        .returning(MedicationReminder)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
