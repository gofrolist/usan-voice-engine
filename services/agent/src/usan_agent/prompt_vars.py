"""Token-scoped {{variable}} substitution for agent prompts (admin-ui Phase 2).

This is the agent-side substitution engine plus a MIRROR of the API's authoritative
variable catalog. `apps/api` and `services/agent` must not import each other
(CLAUDE.md), so BUILTIN_NAMES / BUILTIN_DEFAULTS are a deliberate parallel copy of
`apps/api/.../schemas/variable_catalog.py` — keep names/defaults in sync.

`substitute()` is NOT `str.format`: it only replaces `{{name}}` tokens (and the two
legacy single-brace slots `{elder_name}` / `{last_check_in_line}` for back-compat).
Any other `{` or `}` in operator-authored text passes through untouched, so a stray
or hostile brace can never raise KeyError/IndexError or act as a format-string
injection vector (design spec §4.5).
"""

import re
from collections.abc import Mapping

# Mirror of apps/api schemas.variable_catalog.BUILTIN_NAMES / BUILTIN_DEFAULTS.
# Order is documentation-only here; the agent only needs membership + defaults.
BUILTIN_DEFAULTS: dict[str, str] = {
    "first_name": "there",
    "elder_name": "there",
    "call_direction": "",
    "current_time": "",
    "current_date": "",
    "last_check_in": "",
    "last_check_in_line": "",
    "last_mood": "",
    "last_pain": "",
    "today_meds": "",
}
BUILTIN_NAMES: frozenset[str] = frozenset(BUILTIN_DEFAULTS)

# `{{ name }}` with optional inner whitespace around a bare identifier.
TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# Legacy single-brace slots still present in already-published inbound templates.
# Only these two are resolved; every other `{x}` passes through untouched.
_LEGACY_SLOTS = ("elder_name", "last_check_in_line")


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
