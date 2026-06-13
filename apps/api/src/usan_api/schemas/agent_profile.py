import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.schemas.agent_config import AgentConfig

NAME_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 1000
NOTE_MAX_LENGTH = 500


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    clone_from: uuid.UUID | None = None


class DraftUpdate(BaseModel):
    config: AgentConfig
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    # Optimistic concurrency (FR-032): the revision the editor loaded. When present
    # and != the current draft_revision, the guarded UPDATE matches 0 rows -> 409.
    # Omitted -> unconditional save (backward compatible); the editor always sends it.
    expected_revision: int | None = None


class PublishRequest(BaseModel):
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class SetDefaultRequest(BaseModel):
    direction: Literal["inbound", "outbound"]


class ProfileSummary(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: ProfileStatus
    is_default_inbound: bool
    is_default_outbound: bool
    published_version: int | None
    has_unpublished_draft: bool
    assigned_elder_count: int
    draft_revision: int
    updated_at: datetime

    @classmethod
    def from_model(
        cls,
        profile: AgentProfile,
        *,
        has_unpublished_draft: bool,
        assigned_elder_count: int,
    ) -> ProfileSummary:
        return cls(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            status=profile.status,
            is_default_inbound=profile.is_default_inbound,
            is_default_outbound=profile.is_default_outbound,
            published_version=profile.published_version,
            has_unpublished_draft=has_unpublished_draft,
            assigned_elder_count=assigned_elder_count,
            draft_revision=profile.draft_revision,
            updated_at=profile.updated_at,
        )


class ProfileDetail(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: ProfileStatus
    is_default_inbound: bool
    is_default_outbound: bool
    published_version: int | None
    draft_config: AgentConfig
    # Optimistic-concurrency token (FR-032): the editor loads this with the draft
    # and echoes it back as DraftUpdate.expected_revision on save.
    draft_revision: int
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    # Additive (design §5.1): non-fatal unknown-{{var}} names found in the saved
    # prompts. Defaults to [] so GET responses and older clients are unaffected.
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_model(
        cls, profile: AgentProfile, *, warnings: list[str] | None = None
    ) -> ProfileDetail:
        return cls(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            status=profile.status,
            is_default_inbound=profile.is_default_inbound,
            is_default_outbound=profile.is_default_outbound,
            published_version=profile.published_version,
            draft_config=AgentConfig.model_validate(profile.draft_config),
            draft_revision=profile.draft_revision,
            created_by=profile.created_by,
            updated_by=profile.updated_by,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            warnings=warnings or [],
        )


class VersionSummary(BaseModel):
    version: int
    note: str | None
    published_by: str | None
    published_at: datetime

    @classmethod
    def from_model(cls, version: AgentProfileVersion) -> VersionSummary:
        return cls(
            version=version.version,
            note=version.note,
            published_by=version.published_by,
            published_at=version.published_at,
        )


class VersionDetail(VersionSummary):
    config: AgentConfig

    @classmethod
    def from_model(cls, version: AgentProfileVersion) -> VersionDetail:
        return cls(
            version=version.version,
            note=version.note,
            published_by=version.published_by,
            published_at=version.published_at,
            config=AgentConfig.model_validate(version.config),
        )
