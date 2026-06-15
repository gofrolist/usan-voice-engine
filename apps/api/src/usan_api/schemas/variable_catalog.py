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


# The built-in variables, in catalog/display order (design §3.1). Keep this
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
    # contact_name is a permanent alias of elder_name (US4 / FR-024): same
    # elder.name source, same "there" default, PHI-free. Kept adjacent so the
    # two builtins stay in lockstep with the agent mirror (prompt_vars.py).
    VariableSpec(
        name="contact_name",
        tier="builtin",
        description="The contact's full name (alias of elder_name).",
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
    # US2 / FR-009: open family tasks to convey on this call (then close). PHI=True —
    # the task text is elder-specific and may reference health, so it is warned if used
    # in a field spoken before identity confirmation or to voicemail.
    VariableSpec(
        name="open_family_tasks",
        tier="builtin",
        description="Open tasks family members asked to pass on, to convey then close.",
        default="",
        example="remind mom to drink water; ask about her doctor visit",
        phi=True,
    ),
    # US3 / FR-005: medications the elder reported NOT taken, to gently re-ask this call.
    # PHI=True — names a medication, health information.
    VariableSpec(
        name="pending_med_reasks",
        tier="builtin",
        description="Medications reported not taken, to gently re-ask whether taken yet.",
        default="",
        example="Lisinopril, Metformin",
        phi=True,
    ),
    # US4 / FR-024: durable memory carried across calls. All PHI=True — they embed
    # elder-specific (often health) context resolved from personal_facts /
    # conversation_summaries, so they are warned if used in a field spoken before
    # identity confirmation or to voicemail.
    VariableSpec(
        name="personal_facts",
        tier="builtin",
        description="Durable facts remembered about the elder (people, routines, preferences).",
        default="",
        example="son Tom lives nearby; walks every morning",
        phi=True,
    ),
    VariableSpec(
        name="last_call_summary",
        tier="builtin",
        description="A short recap of the elder's most recent call.",
        default="",
        example="Chatted about the garden; in good spirits.",
        phi=True,
    ),
    VariableSpec(
        name="open_plans",
        tier="builtin",
        description="Follow-ups the elder mentioned last time, to ask about this call.",
        default="",
        example="water the roses; call the pharmacy",
        phi=True,
    ),
    VariableSpec(
        name="important_dates",
        tier="builtin",
        description="Important dates (birthdays, anniversaries) within a day of today.",
        default="",
        example="her birthday",
        phi=True,
    ),
    # US6 / FR-032: "true" when the elder is due for this month's wellbeing survey, else
    # empty. A scheduling flag (no clinical content), so phi=False — safe to reference even
    # in fields spoken before identity confirmation.
    VariableSpec(
        name="survey_due",
        tier="builtin",
        description="'true' when the elder is due for this month's wellbeing survey.",
        default="",
        example="true",
    ),
)

BUILTIN_NAMES: frozenset[str] = frozenset(v.name for v in BUILTIN_VARIABLES)
BUILTIN_DEFAULTS: dict[str, str] = {v.name: v.default for v in BUILTIN_VARIABLES}

# Built-ins carrying protected health information — used to warn when they appear
# in a prompt field spoken before identity confirmation or to voicemail.
PHI_BUILTIN_NAMES: frozenset[str] = frozenset(v.name for v in BUILTIN_VARIABLES if v.phi)
