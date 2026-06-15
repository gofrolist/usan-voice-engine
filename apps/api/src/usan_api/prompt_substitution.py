"""API-side token-scoped {{variable}} substitution for the text-test LLM path.

`apps/api` and `services/agent` must not import each other (CLAUDE.md / Constitution
I), so this is a deliberate PARALLEL COPY of
``services/agent/src/usan_agent/prompt_vars.py`` (plus the small ``sanitize``
helper it depends on). The text-test endpoint (``routers/admin_profile_tests``)
substitutes admin-supplied synthetic ``sample_vars`` into the draft prompt exactly
as the live agent would before sending it to Vertex AI, so the simulation is
faithful.

Kept in lockstep with the agent copy by ``tests/test_prompt_substitution_parity.py``
(T048), which imports BOTH and asserts identical output on a shared corpus. If you
change one, change the other and keep that contract test green.

``substitute()`` is NOT ``str.format``: it only replaces ``{{name}}`` tokens (and the
two legacy single-brace slots ``{elder_name}`` / ``{last_check_in_line}`` for
back-compat). Any other ``{`` or ``}`` passes through untouched, so a stray or
hostile brace can never raise or act as a format-string injection vector.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Mirror of services/agent prompt_vars.BUILTIN_NAMES / BUILTIN_DEFAULTS (which in
# turn mirrors schemas.variable_catalog). Order is documentation-only; only
# membership + defaults matter at substitution time.
BUILTIN_DEFAULTS: dict[str, str] = {
    "first_name": "there",
    "elder_name": "there",
    # contact_name is a permanent alias of elder_name (US4 / FR-024): same source,
    # same "there" default. Keep adjacent to elder_name and in lockstep with the
    # agent mirror (services/agent prompt_vars.BUILTIN_DEFAULTS).
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
    # US6 / FR-032 — "true" when this month's wellbeing survey is still due; not PHI.
    "survey_due": "",
}
BUILTIN_NAMES: frozenset[str] = frozenset(BUILTIN_DEFAULTS)

# `{{ name }}` with optional inner whitespace around a bare identifier. Mirrors
# services/agent prompt_vars.TOKEN_RE and schemas.agent_config._TOKEN_RE.
TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# Legacy single-brace slots still present in already-published inbound templates.
# Only these two are resolved; every other `{x}` passes through untouched.
_LEGACY_SLOTS = ("elder_name", "last_check_in_line")

# Control chars + format-slot braces + invisible/directional chars stripped from any
# admin-supplied string before it reaches an LLM prompt. Mirrors
# services/agent sanitize._PROMPT_UNSAFE byte-for-byte.
_PROMPT_UNSAFE = re.compile(r"[{}\x00-\x1f\x7f\x85­​-‏  ‪-‮⁠-⁤﻿]")


def sanitize_prompt_value(value: Any, *, max_len: int) -> str:
    """Neutralize a supplied string for safe interpolation into LLM instructions.

    Parallel copy of ``usan_agent.sanitize.sanitize_prompt_value``: strips
    format-slot braces and control characters (including newlines), collapses
    surrounding whitespace, and caps the length so a hostile value can neither
    inject new instructions nor introduce ``str.format`` slots.
    """
    text = _PROMPT_UNSAFE.sub(" ", str(value))
    text = " ".join(text.split())
    return text[:max_len].strip()


def substitute(text: str, values: Mapping[str, str]) -> str:
    """Replace `{{name}}` (and the two legacy single-brace slots) from ``values``.

    Unknown / missing names resolve to "" (never left as literal braces). A single
    non-recursive pass, so brace-looking characters inside an inserted value are not
    re-interpreted. Never raises.
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

    Returns ("", "") when the timezone is empty or invalid.
    """
    if not timezone:
        return "", ""
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError, KeyError:
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
    """Merge the variable map the text-test substitutes from.

    Precedence: defaults < sanitized custom (non-builtin names only) < resolved
    built-ins. current_time / current_date are computed from ``timezone``. Any name
    whose final value is empty falls back to its catalog default. Parallel copy of
    ``usan_agent.prompt_vars.build_vars`` — keep in lockstep (T048).
    """
    merged: dict[str, str] = dict(BUILTIN_DEFAULTS)

    for name, value in custom.items():
        if name in BUILTIN_NAMES:
            continue
        merged[name] = sanitize_prompt_value(value, max_len=_INJECTED_VALUE_MAX_LEN)

    for name, value in resolved.items():
        merged[name] = sanitize_prompt_value(value, max_len=_INJECTED_VALUE_MAX_LEN)

    current_time, current_date = _clock(timezone, now)
    merged["current_time"] = current_time
    merged["current_date"] = current_date

    if not merged.get("last_check_in_line"):
        last = merged.get("last_check_in") or ""
        merged["last_check_in_line"] = (
            f"For context, their last check-in was {last}.\n" if last else ""
        )

    for name, default in BUILTIN_DEFAULTS.items():
        if not merged.get(name):
            merged[name] = default
    return merged
