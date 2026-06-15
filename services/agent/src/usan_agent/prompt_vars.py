"""Token-scoped {{variable}} substitution for agent prompts (admin-ui Phase 2).

This is the agent-side substitution engine plus a MIRROR of the API's authoritative
variable catalog. `apps/api` and `services/agent` must not import each other
(CLAUDE.md), so BUILTIN_NAMES / BUILTIN_DEFAULTS are a deliberate parallel copy of
`apps/api/.../schemas/variable_catalog.py` — keep names/defaults in sync.

`substitute()` is NOT `str.format`: it only replaces `{{name}}` tokens (and the two
legacy single-brace slots `{contact_name}` / `{last_check_in_line}` for back-compat).
Any other `{` or `}` in operator-authored text passes through untouched, so a stray
or hostile brace can never raise KeyError/IndexError or act as a format-string
injection vector (design spec §4.5).
"""

import re
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from usan_agent.sanitize import sanitize_prompt_value

# Mirror of apps/api schemas.variable_catalog.BUILTIN_NAMES / BUILTIN_DEFAULTS.
# Order is documentation-only here; the agent only needs membership + defaults.
BUILTIN_DEFAULTS: dict[str, str] = {
    "first_name": "there",
    "contact_name": "there",
    "call_direction": "",
    "current_time": "",
    "current_date": "",
    "last_check_in": "",
    "last_check_in_line": "",
    "last_mood": "",
    "last_pain": "",
    "today_meds": "",
    "open_family_tasks": "",  # US2 / FR-009 — open family tasks to convey this call
    "pending_med_reasks": "",  # US3 / FR-005 — meds reported not-taken, to re-ask
    # US4 / FR-024 — durable memory carried across calls (resolved API-side).
    "personal_facts": "",
    "last_call_summary": "",
    "open_plans": "",
    "important_dates": "",
    # US6 / FR-032 — "true" when the contact is due for this month's wellbeing survey
    # (resolved API-side from wellbeing_survey_results); not PHI.
    "survey_due": "",
}
BUILTIN_NAMES: frozenset[str] = frozenset(BUILTIN_DEFAULTS)

# `{{ name }}` with optional inner whitespace around a bare identifier.
TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# Legacy single-brace slots still present in already-published inbound templates.
# Only these two are resolved; every other `{x}` passes through untouched.
_LEGACY_SLOTS = ("contact_name", "last_check_in_line")


def substitute(text: str, values: Mapping[str, str]) -> str:
    """Replace `{{name}}` (and the two legacy single-brace slots) from ``values``.

    Unknown / missing names resolve to "" (never left as literal braces). This is a
    single non-recursive pass, so brace-looking characters inside an inserted value
    are not re-interpreted. Never raises.
    """

    def _double(match: re.Match[str]) -> str:
        return values.get(match.group(1), "")

    out = TOKEN_RE.sub(_double, text)
    for slot in _LEGACY_SLOTS:
        if slot in values:
            out = out.replace("{" + slot + "}", values[slot])
    return out


_INJECTED_VALUE_MAX_LEN = 300  # caps every injected value (resolved built-in + custom)
_TIME_FMT = "%-I:%M %p"  # "9:15 AM"
_DATE_FMT = "%A, %B %-d"  # "Monday, June 8"


def _clock(timezone: str, now: datetime) -> tuple[str, str]:
    """Localize ``now`` to ``timezone`` and format current_time / current_date.

    Returns ("", "") when the timezone is empty or invalid, so a missing/garbled
    tz never crashes the call and simply blanks the two clock variables.
    """
    if not timezone:
        return "", ""
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "", ""
    local = now.astimezone(tz)
    return local.strftime(_TIME_FMT), local.strftime(_DATE_FMT)


def build_vars(
    resolved: Mapping[str, str],
    custom: Mapping[str, object],
    *,
    timezone: str,
    now: datetime,
) -> dict[str, str]:
    """Merge the per-call variable map the agent substitutes from.

    Precedence (design spec §4.4): defaults < sanitized custom (non-builtin names
    only) < resolved built-ins. current_time / current_date are computed agent-side
    from ``timezone``. Any name whose final value is empty falls back to its catalog
    default (so e.g. a blank first_name speaks "there").
    """
    merged: dict[str, str] = dict(BUILTIN_DEFAULTS)

    # Custom (caller-derived) values: only names that are NOT built-ins, sanitized.
    for name, value in custom.items():
        if name in BUILTIN_NAMES:
            continue
        merged[name] = sanitize_prompt_value(value, max_len=_INJECTED_VALUE_MAX_LEN)

    # Resolved built-ins win over custom/defaults. They are STILL injected values
    # derived from contact/call data (e.g. last_check_in embeds WellnessLog.notes —
    # contact-spoken, transcribed text), so they are sanitized here too before being
    # woven into the prompt (design spec §4.5). The agent is the trust boundary
    # nearest the LLM; sanitizing here is defense-in-depth regardless of the API.
    for name, value in resolved.items():
        merged[name] = sanitize_prompt_value(value, max_len=_INJECTED_VALUE_MAX_LEN)

    current_time, current_date = _clock(timezone, now)
    merged["current_time"] = current_time
    merged["current_date"] = current_date

    # Derive last_check_in_line from last_check_in when not already supplied.
    # This keeps the single-source logic here (owned by the defaults table / catalog)
    # so it applies for both outbound and inbound calls, not just the inbound builder.
    if not merged.get("last_check_in_line"):
        last = merged.get("last_check_in") or ""
        merged["last_check_in_line"] = (
            f"For context, their last check-in was {last}.\n" if last else ""
        )

    # Empty/None falls back to the catalog default for that name.
    for name, default in BUILTIN_DEFAULTS.items():
        if not merged.get(name):
            merged[name] = default
    return merged
