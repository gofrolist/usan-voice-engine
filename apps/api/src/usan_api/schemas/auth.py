import uuid

from pydantic import BaseModel, Field

# Minimal email regex avoids adding the email-validator dependency that EmailStr needs.
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class OrgSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str | None = None  # the caller's role in this org (None for act-as-only super-admin)


class MeResponse(BaseModel):
    email: str
    is_super_admin: bool
    acting_as: bool
    active_org: OrgSummary | None
    orgs: list[OrgSummary]


class SwitchOrgRequest(BaseModel):
    organization_id: uuid.UUID


class AdminUserOut(BaseModel):
    email: str
    role: str
    added_by: str | None = None


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")
