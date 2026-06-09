"""Render an SMS template body with the call's NON-PHI variables (design §6.3, §9).

A template body may reference only non-PHI catalog variables (PHI tokens are
hard-blocked at save in agent_config._sms_templates_no_phi). Here, as
defense-in-depth, we (1) resolve the builtin vars, (2) DROP every PHI name, (3)
add the two runtime clock vars, (4) pass each value through a LOCAL sanitize
(strip control chars / braces / zero-width) BEFORE substitution, and (5) replace
unknown tokens with the empty string. Substitution is token-scoped via _TOKEN_RE,
never str.format, so a hostile value cannot inject a new slot.
"""

import re
from datetime import datetime
from typing import Any, Literal

from usan_api import builtin_vars
from usan_api.schemas.agent_config import _TOKEN_RE
from usan_api.schemas.variable_catalog import PHI_BUILTIN_NAMES

# Mirrors services/agent sanitize._PROMPT_UNSAFE (kept local: apps/api must not
# import services/agent). Strips format-slot braces, ASCII control chars, the
# Unicode line/paragraph separators, and invisible/directional chars.
_VALUE_UNSAFE = re.compile(
    r"[{}\x00-\x1f\x7f\x85\u00ad\u200b-\u200f\u2028-\u2029\u202a-\u202e\u2060-\u2064\ufeff]"
)
_VALUE_MAX_LEN = 160


def _sanitize(value: str) -> str:
    text = _VALUE_UNSAFE.sub(" ", value)
    text = " ".join(text.split())
    return text[:_VALUE_MAX_LEN].strip()


def _clock_vars(elder: Any, now: datetime) -> dict[str, str]:
    """current_time / current_date in the elder's timezone (best-effort)."""
    from zoneinfo import ZoneInfo

    tz = getattr(elder, "timezone", "") or "UTC"
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:
        local = now
    return {
        "current_time": local.strftime("%-I:%M %p").lstrip("0"),
        "current_date": local.strftime("%A, %B %-d"),
    }


def render_sms_body(
    template_body: str, *, call: Any, elder: Any, now: datetime | None = None
) -> str:
    """Substitute non-PHI {{tokens}} in ``template_body`` for one call.

    ``now`` is injectable for testing; the endpoint calls
    ``render_sms_body(template.body, call=call, elder=elder)``.
    """
    when = now or datetime.now()
    raw_direction = getattr(getattr(call, "direction", None), "value", "outbound")
    direction: Literal["inbound", "outbound"] = (
        "inbound" if raw_direction == "inbound" else "outbound"
    )
    resolved, _tz = builtin_vars.resolve_builtin_vars(elder, None, direction=direction)
    values = {k: v for k, v in resolved.items() if k not in PHI_BUILTIN_NAMES}
    values.update(_clock_vars(elder, when))

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        raw = values.get(name, "")
        return _sanitize(raw) if raw else ""

    return _TOKEN_RE.sub(_replace, template_body)
