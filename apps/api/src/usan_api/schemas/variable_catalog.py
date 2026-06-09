"""The dynamic-prompt variable catalog (Admin-UI Phase 2 design §3).

This module is the AUTHORITATIVE definition of the built-in variable tier. The
agent holds a hand-mirrored copy of BUILTIN_NAMES / BUILTIN_DEFAULTS (the same
parallel-copy pattern as services/agent/.../agent_config.py mirrors AgentConfig),
and the admin-ui fetches the full list at runtime from GET
/v1/admin/variable-catalog. The catalog is a GLOBAL constant, NOT a per-version
snapshot, so it never participates in the agent_profile_versions forward-compat
invariant.
"""

from typing import Literal

from pydantic import BaseModel


class VariableSpec(BaseModel):
    """One catalog variable: how the editor describes it and what it defaults to."""

    name: str  # no braces, e.g. "first_name"
    tier: Literal["builtin", "custom"]
    description: str
    default: str  # "" when there is no default
    example: str
    # phi=True marks protected health information. The admin-ui uses this flag to
    # render a non-blocking warning when a PHI variable appears in a sensitive prompt
    # field (e.g. greeting, voicemail_message) that may be spoken before the caller's
    # identity is confirmed or to an answering machine.
    phi: bool = False


# The 10 built-in variables, in catalog/display order (design §3.1). Keep this
# list and the agent-side mirror (services/agent/.../prompt_vars.py) in lockstep.
BUILTIN_VARIABLES: tuple[VariableSpec, ...] = (
    VariableSpec(
        name="first_name",
        tier="builtin",
        description="The elder's first name (first word of their full name).",
        default="there",
        example="Margaret",
    ),
    VariableSpec(
        name="elder_name",
        tier="builtin",
        description="The elder's full name.",
        default="there",
        example="Margaret Doe",
    ),
    VariableSpec(
        name="call_direction",
        tier="builtin",
        description="Whether this call is 'inbound' or 'outbound'.",
        default="",
        example="outbound",
    ),
    VariableSpec(
        name="current_time",
        tier="builtin",
        description="Current local time in the elder's timezone.",
        default="",
        example="9:15 AM",
    ),
    VariableSpec(
        name="current_date",
        tier="builtin",
        description="Today's local date in the elder's timezone.",
        default="",
        example="Monday, June 8",
    ),
    VariableSpec(
        name="last_check_in",
        tier="builtin",
        description="Summary of the elder's most recent wellness check-in.",
        default="",
        example="on 2026-06-05, mood 4/5, pain 2/10",
        phi=True,
    ),
    VariableSpec(
        name="last_check_in_line",
        tier="builtin",
        description="A ready-made sentence about the last check-in, or empty if none.",
        default="",
        example="For context, their last check-in was on 2026-06-05, mood 4/5.",
        phi=True,
    ),
    VariableSpec(
        name="last_mood",
        tier="builtin",
        description="The elder's most recent mood rating (1-5).",
        default="",
        example="4",
        phi=True,
    ),
    VariableSpec(
        name="last_pain",
        tier="builtin",
        description="The elder's most recent pain level (0-10).",
        default="",
        example="2",
        phi=True,
    ),
    VariableSpec(
        name="today_meds",
        tier="builtin",
        description="Comma-separated names of the elder's medications scheduled today.",
        default="",
        example="Lisinopril, Metformin",
        phi=True,
    ),
)

BUILTIN_NAMES: frozenset[str] = frozenset(v.name for v in BUILTIN_VARIABLES)
BUILTIN_DEFAULTS: dict[str, str] = {v.name: v.default for v in BUILTIN_VARIABLES}
