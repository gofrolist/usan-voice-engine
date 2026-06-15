"""Read-only Defaults-area response (US3 / FR-016..020).

The Defaults area states, per direction, which profile is the current default and
whether it is still effective, explains the resolution order in plain language, and
exposes the built-in last-resort fallback config read-only. Everything here is
names/ids/config — never per-call PHI (spec §7).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig

# Plain-language resolution order (highest precedence first). Mirrors
# repositories.agent_profiles.resolve_agent_config's precedence walk
# (override -> contact/contact assignment -> per-direction default -> built-in
# fallback). Names only — no values (FR-017).
RESOLUTION_ORDER: tuple[str, ...] = (
    "Per-call profile override",
    "Per-contact assignment",
    "Per-direction default profile",
    "Built-in fallback configuration",
)

# Why a flagged default is no longer effective (FR-020). None when it resolves.
IneligibleReason = Literal["archived", "unpublished"]


class DefaultProfileRef(BaseModel):
    """The profile currently flagged default for a direction (name/id only)."""

    id: uuid.UUID
    name: str
    status: ProfileStatus
    published_version: int | None
    # True iff this default actually resolves for a call (ACTIVE + published).
    eligible: bool

    @classmethod
    def from_model(cls, profile: AgentProfile) -> DefaultProfileRef:
        eligible = profile.status == ProfileStatus.ACTIVE and profile.published_version is not None
        return cls(
            id=profile.id,
            name=profile.name,
            status=profile.status,
            published_version=profile.published_version,
            eligible=eligible,
        )


class DirectionDefault(BaseModel):
    """Per-direction default state for the Defaults area (FR-016)."""

    direction: Literal["inbound", "outbound"]
    # None when no profile is flagged default for this direction.
    default_profile: DefaultProfileRef | None
    # True when a default IS flagged but no longer effective (archived/unpublished).
    ineligible: bool
    ineligible_reason: IneligibleReason | None = None

    @classmethod
    def from_holder(
        cls, direction: Literal["inbound", "outbound"], holder: AgentProfile | None
    ) -> DirectionDefault:
        if holder is None:
            return cls(direction=direction, default_profile=None, ineligible=False)
        ref = DefaultProfileRef.from_model(holder)
        reason: IneligibleReason | None = None
        if not ref.eligible:
            reason = "archived" if holder.status == ProfileStatus.ARCHIVED else "unpublished"
        return cls(
            direction=direction,
            default_profile=ref,
            ineligible=not ref.eligible,
            ineligible_reason=reason,
        )


class DefaultsResponse(BaseModel):
    """The whole Defaults-area read model (FR-016..020)."""

    directions: list[DirectionDefault]
    resolution_order: list[str] = Field(default_factory=lambda: list(RESOLUTION_ORDER))
    # The built-in last-resort fallback config, read-only (FR-017/FR-019).
    builtin_fallback: AgentConfig = DEFAULT_AGENT_CONFIG
