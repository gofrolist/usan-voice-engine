"""The agent tool catalog (Admin-UI Phase 3 design §4.1).

This module is the AUTHORITATIVE definition of the agent's tool inventory. Unlike
the variable catalog (an open-ended set where unknown names warn-don't-block), the
tool catalog is a CLOSED set: ``schemas/agent_config.ToolsConfig._known_tools``
imports ``TOOL_NAMES`` from here and HARD-BLOCKS unknown tool names. The agent
holds a hand-mirrored ``_TOOL_REGISTRY`` (services/agent/.../check_in.py); the
admin-ui fetches the full list at runtime from GET /v1/admin/tool-catalog. The
catalog is a GLOBAL constant, NOT a per-version snapshot, so it never participates
in the agent_profile_versions forward-compat invariant.

The catalog is the SUPERSET: a tool may appear here (so it validates and renders in
the editor) before its agent-side ``@function_tool`` callable exists. The agent
``_select_tools`` silently drops any enabled name that is not yet in its registry, so
enabling such a tool saves successfully but is a no-op until the agent catches up.
This is a deliberate rollout property -- the two sides converge per the design's
catalog<->registry sync test once every tool's callable lands (Parts B/C/D).
"""

from typing import Literal

from pydantic import BaseModel


class ToolSpec(BaseModel):
    """One catalog tool: how the editor describes it and how it is gated."""

    name: str  # registry key, e.g. "flag_for_followup"
    label: str  # human label for the UI
    description: str  # what it does (shown in the editor)
    # Closed set, enforced at instantiation: "logging" writes/reads call data;
    # "lifecycle"/"safety"/"messaging" gate call termination, human escalation,
    # and outbound SMS. A typo or undocumented value fails model construction.
    category: Literal["logging", "lifecycle", "safety", "messaging"]
    # end_call is locked on (cannot be disabled): it drives the only graceful
    # report->goodbye->delete_room->shutdown path.
    always_on: bool = False
    # send_sms needs >=1 SMS template before the agent offers it to the LLM
    # (an enabled-but-template-less send_sms is a dead tool).
    requires_config: bool = False


# The 15 catalog tools, in catalog/display order (design §4.1). This is the superset;
# the agent-side mirror (services/agent/.../check_in.py _TOOL_REGISTRY) catches up as
# each tool's @function_tool callable lands (B/C/D) and may legitimately trail it.
TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="log_wellness",
        label="Log wellness",
        description="Record the contact's mood, pain level, and a short note for this call.",
        category="logging",
    ),
    ToolSpec(
        name="log_medication",
        label="Log medication",
        description="Record whether the contact has taken a specific medication.",
        category="logging",
    ),
    ToolSpec(
        name="get_today_meds",
        label="Get today's medications",
        description="Read back the medications the contact is scheduled to take today.",
        category="logging",
    ),
    ToolSpec(
        name="flag_for_followup",
        label="Flag for follow-up",
        description="Raise a safety-escalation flag for a human to review after the call.",
        category="safety",
    ),
    ToolSpec(
        name="raise_crisis",
        label="Raise crisis",
        description=(
            "Escalate a life-safety crisis (suicidal, medical, abuse, confusion, overdose): "
            "record an urgent flag, return the emergency resource to read out, and alert family."
        ),
        category="safety",
    ),
    ToolSpec(
        name="schedule_callback",
        label="Schedule callback",
        description="Record a call-back request in the contact's words for a human to action.",
        category="safety",
    ),
    ToolSpec(
        name="close_family_task",
        label="Close family task",
        description=(
            "After conveying a family member's message to the contact, mark it delivered so "
            "it is not repeated on the next call."
        ),
        category="logging",
    ),
    ToolSpec(
        name="record_personal_fact",
        label="Record personal fact",
        description=(
            "Remember a durable fact the contact shared (a person, routine, preference, "
            "important date, or health context) to use naturally on future calls."
        ),
        category="logging",
    ),
    ToolSpec(
        name="record_survey",
        label="Record wellbeing survey",
        description=(
            "Record the contact's monthly wellbeing survey (loneliness, mood, satisfaction). "
            "Idempotent: at most one survey per contact per calendar month."
        ),
        category="logging",
    ),
    ToolSpec(
        name="get_activity",
        label="Get mood-boosting activity",
        description=(
            "Fetch a mood-boosting activity (breathing, memory, or light game) not used "
            "recently, to offer when the contact's mood is low."
        ),
        category="logging",
    ),
    ToolSpec(
        name="send_sms",
        label="Send SMS",
        description="Send an operator-authored, non-PHI templated text after the call.",
        category="messaging",
        requires_config=True,
    ),
    ToolSpec(
        name="send_info_sms",
        label="Send helpful numbers (SMS)",
        description=(
            "Text the contact a fixed, PHI-free list of helpful emergency and helpline "
            "phone numbers on request. Needs no template."
        ),
        category="messaging",
    ),
    ToolSpec(
        name="register_opt_out",
        label="Register opt-out",
        description=(
            "Honor a spoken request to stop calls: add the contact's number to the "
            "do-not-call list, acknowledge it, and alert an operator."
        ),
        category="safety",
    ),
    ToolSpec(
        name="set_spanish_callback",
        label="Set Spanish callback",
        description=(
            "When the contact speaks Spanish, record the language preference and schedule a "
            "Spanish-language callback (Clara does not switch languages mid-call)."
        ),
        category="safety",
    ),
    ToolSpec(
        name="end_call",
        label="End call",
        description="End the call gracefully once the check-in is complete.",
        category="lifecycle",
        always_on=True,
    ),
)

# Closed set of known tool names — schemas/agent_config.ToolsConfig imports this and
# hard-blocks anything outside it (design §3.1, §4.1).
TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOL_CATALOG)


class ToolCatalogResponse(BaseModel):
    # Lives in the schema module (not inline in admin_tool_catalog.py the way
    # admin_variable_catalog.VariableCatalogResponse does) because it is part of this
    # module's unit-tested A1 contract (test_tool_catalog.test_catalog_response_*).
    tools: list[ToolSpec]
