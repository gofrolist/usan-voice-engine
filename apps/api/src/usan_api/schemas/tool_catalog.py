"""The agent tool catalog (Admin-UI Phase 3 design §4.1).

This module is the AUTHORITATIVE definition of the agent's tool inventory. Unlike
the variable catalog (an open-ended set where unknown names warn-don't-block), the
tool catalog is a CLOSED set: ``schemas/agent_config.ToolsConfig._known_tools``
imports ``TOOL_NAMES`` from here and HARD-BLOCKS unknown tool names. The agent
holds a hand-mirrored ``_TOOL_REGISTRY`` (services/agent/.../check_in.py); the
admin-ui fetches the full list at runtime from GET /v1/admin/tool-catalog. The
catalog is a GLOBAL constant, NOT a per-version snapshot, so it never participates
in the agent_profile_versions forward-compat invariant.
"""

from pydantic import BaseModel


class ToolSpec(BaseModel):
    """One catalog tool: how the editor describes it and how it is gated."""

    name: str  # registry key, e.g. "flag_for_followup"
    label: str  # human label for the UI
    description: str  # what it does (shown in the editor)
    category: str  # "logging" | "lifecycle" | "safety" | "messaging"
    # end_call is locked on (cannot be disabled): it drives the only graceful
    # report->goodbye->delete_room->shutdown path.
    always_on: bool = False
    # send_sms needs >=1 SMS template before the agent offers it to the LLM
    # (an enabled-but-template-less send_sms is a dead tool).
    requires_config: bool = False


# The 7 catalog tools, in catalog/display order (design §4.1). Keep this list and
# the agent-side mirror (services/agent/.../check_in.py _TOOL_REGISTRY) in lockstep.
TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="log_wellness",
        label="Log wellness",
        description="Record the elder's mood, pain level, and a short note for this call.",
        category="logging",
    ),
    ToolSpec(
        name="log_medication",
        label="Log medication",
        description="Record whether the elder has taken a specific medication.",
        category="logging",
    ),
    ToolSpec(
        name="get_today_meds",
        label="Get today's medications",
        description="Read back the medications the elder is scheduled to take today.",
        category="logging",
    ),
    ToolSpec(
        name="flag_for_followup",
        label="Flag for follow-up",
        description="Raise a safety-escalation flag for a human to review after the call.",
        category="safety",
    ),
    ToolSpec(
        name="schedule_callback",
        label="Schedule callback",
        description="Record a call-back request in the elder's words for a human to action.",
        category="safety",
    ),
    ToolSpec(
        name="send_sms",
        label="Send SMS",
        description="Send an operator-authored, non-PHI templated text after the call.",
        category="messaging",
        requires_config=True,
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
    tools: list[ToolSpec]
