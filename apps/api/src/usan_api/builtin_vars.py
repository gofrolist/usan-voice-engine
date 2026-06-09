"""Resolve the data-tier built-in variables for a call (Admin-UI Phase 2, contract C).

The agent has no database, so the API resolves the 8 DATA built-ins from the loaded
elder + latest WellnessLog + the elder's medication schedule, and passes the elder's
IANA timezone alongside. The two runtime CLOCK built-ins (current_time/current_date)
are resolved agent-side at call answer and are intentionally NOT produced here.

These values are passed OUT-OF-BAND (inbound response / outbound job metadata) and
MUST NOT be written into the persisted Call.dynamic_vars, which is the outbound
idempotency payload (design §4.3).
"""

from typing import Any, Literal

from usan_api.db.models import Elder, WellnessLog

# The 8 data built-ins this resolver emits (contract C). current_time/current_date
# are deliberately excluded — the agent adds them.
DATA_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        "first_name",
        "elder_name",
        "call_direction",
        "last_check_in",
        "last_check_in_line",
        "last_mood",
        "last_pain",
        "today_meds",
    }
)


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
) -> tuple[dict[str, str], str]:
    """Resolve the 8 data built-ins + the elder's timezone.

    Returns ``(resolved_vars, timezone)``. Every value is a plain string; a missing
    source yields ``""`` (the agent applies catalog defaults). An unknown caller
    (``elder is None``) still resolves ``call_direction`` and blanks the rest.
    """
    resolved: dict[str, str] = {
        "first_name": "",
        "elder_name": "",
        "call_direction": direction,
        "last_check_in": "",
        "last_check_in_line": "",
        "last_mood": "",
        "last_pain": "",
        "today_meds": "",
    }
    timezone = ""
    if elder is not None:
        full = elder.name or ""
        resolved["elder_name"] = full
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
