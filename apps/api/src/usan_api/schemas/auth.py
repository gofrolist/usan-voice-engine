import uuid

from pydantic import BaseModel


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
