"""Resolve the data-tier built-in variables for a call (Admin-UI Phase 2, contract C).

The agent has no database, so the API resolves the 8 DATA built-ins from the loaded
elder + latest WellnessLog + the elder's medication schedule, and passes the elder's
IANA timezone alongside. The two runtime CLOCK built-ins (current_time/current_date)
are resolved agent-side at call answer and are intentionally NOT produced here.

These values are passed OUT-OF-BAND (inbound response / outbound job metadata) and
MUST NOT be written into the persisted Call.dynamic_vars, which is the outbound
idempotency payload (design §4.3).
"""

import calendar
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from usan_api.db.models import ConversationSummary, Elder, PersonalFact, WellnessLog

# The 8 data built-ins this resolver emits (contract C). current_time/current_date
# are deliberately excluded — the agent adds them.
DATA_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        "first_name",
        "elder_name",
        "contact_name",  # US4 alias of elder_name (FR-024) — same elder.name source
        "call_direction",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
        "open_family_tasks",
        "pending_med_reasks",  # US3 / FR-005 — meds reported not-taken, to re-ask
        # US4 / FR-024 — durable memory carried across calls. Resolved API-side from the
        # elder's active personal_facts + most recent conversation_summary.
        "personal_facts",
        "last_call_summary",
        "open_plans",
        "important_dates",
        # US6 / FR-032 — "true" when the elder is due for this month's wellbeing survey
        # (no wellbeing_survey_results row for the local month yet), else "".
        "survey_due",
    }
)


# How many days either side of today counts an important_date as "coming up" (data-model).
_IMPORTANT_DATE_WINDOW_DAYS = 1


def format_last_check_in(log: Any) -> str:
    """A short human summary of the elder's most recent wellness log.

    Moved here from routers.calls so both inbound and outbound resolution share one
    implementation; routers.calls re-exports it for back-compat.
    """
    parts = [f"on {log.logged_at.date().isoformat()}"]
    if log.mood is not None:
        parts.append(f"mood {log.mood}/5")
    if log.pain_level is not None:
        parts.append(f"pain {log.pain_level}/10")
    summary = ", ".join(parts)
    if log.notes:
        summary += f" — note: {log.notes}"
    return summary


def _last_check_in_line(log: Any) -> str:
    """The legacy pre-formatted sentence (matches the old inbound template slot)."""
    return f"For context, their last check-in was {format_last_check_in(log)}."


def _today_meds(elder: Any) -> str:
    """Comma-join the names of the elder's scheduled meds (same source as get_today_meds)."""
    raw = elder.meta.get("medication_schedule", [])
    if not isinstance(raw, list):
        return ""
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return ", ".join(names)


def resolve_builtin_vars(
    elder: Elder | None,
    last_log: WellnessLog | None,
    *,
    direction: Literal["inbound", "outbound"],
    open_family_tasks: list[str] | None = None,
    pending_med_reasks: list[str] | None = None,
    personal_facts: list[str] | None = None,
    last_call_summary: str | None = None,
    open_plans: list[str] | None = None,
    important_dates: list[str] | None = None,
    survey_due: bool = False,
) -> tuple[dict[str, str], str]:
    """Resolve the data built-ins + the elder's timezone.

    Returns ``(resolved_vars, timezone)``. Every value is a plain string; a missing
    source yields ``""`` (the agent applies catalog defaults). An unknown caller
    (``elder is None``) still resolves ``call_direction`` and blanks the rest.
    ``open_family_tasks`` (US2 / FR-009) and ``pending_med_reasks`` (US3 / FR-005) are
    passed in by the caller (which has the DB session) as the list of open task messages /
    not-taken medication names; the resolver renders them prompt-ready. Kept plain params —
    not DB queries here — so this function stays pure/sync and the SMS-render caller can
    skip them (passes None → blank).
    """
    resolved: dict[str, str] = {
        "first_name": "",
        "elder_name": "",
        "contact_name": "",  # US4 alias of elder_name (FR-024)
        "call_direction": direction,
        "last_check_in": "",
        "last_check_in_line": "",
        "last_mood": "",
        "last_pain": "",
        "today_meds": "",
        # Joined with "; " (tasks are full phrases that may contain commas, unlike
        # today_meds' single-token names). Empty when there are no open tasks.
        "open_family_tasks": "; ".join(m for m in (open_family_tasks or []) if m.strip()),
        # Med names reported not-taken (US3 / FR-005). Single tokens like today_meds, so
        # comma-joined. Passed in by the caller (which has the DB session); empty when none.
        "pending_med_reasks": ", ".join(m for m in (pending_med_reasks or []) if m.strip()),
        # US4 / FR-024 memory built-ins. Facts/plans are full phrases (";"-joined like
        # open_family_tasks); important_dates are short labels (","-joined like today_meds).
        # All are PHI and pass through the agent's build_vars sanitizer + 300-char cap.
        "personal_facts": "; ".join(f for f in (personal_facts or []) if f.strip()),
        "last_call_summary": (last_call_summary or "").strip(),
        "open_plans": "; ".join(p for p in (open_plans or []) if p.strip()),
        "important_dates": ", ".join(d for d in (important_dates or []) if d.strip()),
        # US6 / FR-032 — a scheduling flag, not PHI. "true" only when caller-computed
        # survey_due is set; the agent uses it to decide whether to run the monthly survey.
        "survey_due": "true" if survey_due else "",
    }
    timezone = ""
    if elder is not None:
        full = elder.name or ""
        resolved["elder_name"] = full
        # contact_name aliases elder_name from the same elder.name source (FR-024).
        resolved["contact_name"] = full
        resolved["first_name"] = full.split()[0] if full.split() else ""
        resolved["today_meds"] = _today_meds(elder)
        timezone = elder.timezone or ""
    if last_log is not None:
        resolved["last_check_in"] = format_last_check_in(last_log)
        resolved["last_check_in_line"] = _last_check_in_line(last_log)
        if last_log.mood is not None:
            resolved["last_mood"] = str(last_log.mood)
        if last_log.pain_level is not None:
            resolved["last_pain"] = str(last_log.pain_level)
    return resolved, timezone


def _elder_today(timezone: str, now: datetime) -> date:
    """Today's date in the elder's timezone (falls back to ``now``'s date on a bad tz)."""
    if not timezone:
        return now.date()
    try:
        return now.astimezone(ZoneInfo(timezone)).date()
    except ZoneInfoNotFoundError, ValueError, KeyError:
        return now.date()


def build_memory_params(
    facts: list[PersonalFact],
    summary: ConversationSummary | None,
    *,
    timezone: str,
    now: datetime,
) -> dict[str, Any]:
    """Turn the elder's active facts + latest summary into the four memory built-in
    inputs for ``resolve_builtin_vars`` (pure; the caller does the DB load + injects now).

    ``important_date`` facts are projected into ``important_dates`` (only those within
    ±1 day of today, matched on month/day so birthdays/anniversaries recur) and are NOT
    duplicated into ``personal_facts``. ``last_call_summary``/``open_plans`` come from the
    most recent summary.
    """
    today = _elder_today(timezone, now)
    window = {
        ((today + timedelta(days=d)).month, (today + timedelta(days=d)).day)
        for d in range(-_IMPORTANT_DATE_WINDOW_DAYS, _IMPORTANT_DATE_WINDOW_DAYS + 1)
    }
    personal: list[str] = []
    important: list[str] = []
    for fact in facts:
        if fact.category == "important_date":
            structured = fact.structured if isinstance(fact.structured, dict) else {}
            raw = structured.get("date")
            if not isinstance(raw, str):
                continue
            try:
                when = date.fromisoformat(raw)
            except ValueError:
                continue
            month_day = (when.month, when.day)
            # Observe a Feb-29 anniversary on Feb-28 in non-leap years, else it would be
            # surfaced only ~1 year in 4 (the window never contains (2, 29) otherwise).
            if month_day == (2, 29) and not calendar.isleap(today.year):
                month_day = (2, 28)
            if month_day in window:
                label = structured.get("label")
                important.append(
                    str(label) if isinstance(label, str) and label.strip() else fact.content
                )
        elif fact.content and fact.content.strip():
            personal.append(fact.content)

    last_summary = summary.summary if summary is not None else None
    plans: list[str] = []
    if summary is not None and isinstance(summary.open_plans, list):
        plans = [p for p in summary.open_plans if isinstance(p, str) and p.strip()]

    return {
        "personal_facts": personal,
        "last_call_summary": last_summary,
        "open_plans": plans,
        "important_dates": important,
    }
