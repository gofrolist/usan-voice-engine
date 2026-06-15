"""PHI-minimized family/operator notification builder (Clara Care Parity 002).

Creates non-call ``sms_messages`` rows (``kind``/``dedupe_key``) for family alerts,
monthly reports, and opt-out acks. Bodies come from FIXED, PHI-FREE templates — a
family alert says "please check in", never a mood score / medication name / transcript
text (Constitution II; see docs/compliance/clara-care-parity-go-live.md). The
notification outbox poller (notification_outbox.py) delivers them via Telnyx.

All enqueue_* helpers are idempotent on ``dedupe_key`` (e.g. ``crisis:{flag_id}`` /
``missed:{call_id}``): a duplicate completion path cannot text the family twice.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import FamilyContact, SmsMessage
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.repositories import sms_messages as sms_repo

FamilyAlertReason = Literal["crisis", "missed_call"]

# PHI-FREE templates. They intentionally carry NO clinical content: the detail stays in
# Postgres; the SMS only prompts the family contact to reach out.
_FAMILY_ALERT_BODIES: dict[str, str] = {
    "crisis": (
        "This is USAN Retirement. Please check in with your family member as soon as "
        "you can. — USAN"
    ),
    "missed_call": (
        "This is USAN Retirement. We weren't able to reach your family member for their "
        "wellness call. Please check in with them when you can. — USAN"
    ),
}

_OPT_OUT_ACK_BODY = (
    "You've been unsubscribed from USAN Retirement calls and texts. Reply START to resume. — USAN"
)


def build_family_alert_body(reason: FamilyAlertReason) -> str:
    """The PHI-free SMS body for a family alert of the given reason."""
    return _FAMILY_ALERT_BODIES[reason]


def build_opt_out_ack_body() -> str:
    """The PHI-free one-time opt-out acknowledgement body."""
    return _OPT_OUT_ACK_BODY


# PHI-FREE monthly family report SMS (US8 / FR-012). The status-and-trends detail (mood,
# adherence, survey) stays in Postgres on the family_reports row; the SMS only signals that
# the elder is engaged and invites the family to call for specifics (Constitution II / T083).
_FAMILY_REPORT_BODY = (
    "This is USAN Retirement with a monthly update on your loved one. They have been "
    "staying in touch with us through their wellness calls this month. Call us anytime "
    "to talk about how they're doing. — USAN"
)


def build_family_report_body() -> str:
    """The PHI-free monthly family report SMS body (no clinical content)."""
    return _FAMILY_REPORT_BODY


async def enqueue_family_alert(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    to_number: str,
    reason: FamilyAlertReason,
    dedupe_key: str,
) -> SmsMessage | None:
    """Enqueue a PHI-minimized family alert (crisis / missed-call). Idempotent."""
    return await sms_repo.create_notification(
        db,
        elder_id=elder_id,
        to_number=to_number,
        kind="family_alert",
        body=build_family_alert_body(reason),
        dedupe_key=dedupe_key,
    )


@dataclass(frozen=True)
class AlertDispatch:
    """Outcome of resolving + enqueuing a family alert (US2 / T033/T034/T088).

    ``notified`` are the contacts opted in to this alert kind (an SMS was enqueued to
    each). ``had_contacts`` is True if ANY contact is registered for the elder — so a
    caller routes to the operator queue (FR-013) only on a true absence, not when every
    contact merely opted out.
    """

    notified: list[FamilyContact]
    had_contacts: bool


async def dispatch_family_alert(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    reason: FamilyAlertReason,
    dedupe_base: str,
) -> AlertDispatch:
    """Resolve family recipients for ``reason`` and enqueue a PHI-safe alert to each.

    Recipients are the contacts whose ``alert_prefs`` opt in to ``reason`` (fail-open).
    Each alert is idempotent on ``{dedupe_base}:{phone}`` (e.g. ``crisis:{flag_id}`` /
    ``missed:{call_id}``). Returns an :class:`AlertDispatch`; an empty ``notified`` with
    ``had_contacts=False`` is the caller's signal to fall back to the operator queue.
    Flush-only; the caller commits.
    """
    recipients = await family_contacts_repo.list_alert_recipients(
        db, elder_id=elder_id, kind=reason
    )
    for contact in recipients:
        await enqueue_family_alert(
            db,
            elder_id=elder_id,
            to_number=contact.phone_e164,
            reason=reason,
            dedupe_key=f"{dedupe_base}:{contact.phone_e164}",
        )
    # Only pay the extra query when no one was opted in — distinguishes "no contact
    # registered" (operator fallback) from "all contacts opted out" (deliberate silence).
    had_contacts = bool(recipients) or bool(
        await family_contacts_repo.list_family_contacts(db, elder_id=elder_id)
    )
    return AlertDispatch(notified=list(recipients), had_contacts=had_contacts)


async def enqueue_opt_out_ack(
    db: AsyncSession, *, elder_id: uuid.UUID, to_number: str, dedupe_key: str
) -> SmsMessage | None:
    """Enqueue the one-time PHI-free opt-out acknowledgement. Idempotent."""
    return await sms_repo.create_notification(
        db,
        elder_id=elder_id,
        to_number=to_number,
        kind="opt_out_ack",
        body=build_opt_out_ack_body(),
        dedupe_key=dedupe_key,
    )


async def enqueue_family_report(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    to_number: str,
    body: str,
    dedupe_key: str,
) -> SmsMessage | None:
    """Enqueue a monthly family report SMS. The caller supplies a PHI-minimized body
    (the full report narrative stays in Postgres; the SMS only signals an update)."""
    return await sms_repo.create_notification(
        db,
        elder_id=elder_id,
        to_number=to_number,
        kind="family_report",
        body=body,
        dedupe_key=dedupe_key,
    )
