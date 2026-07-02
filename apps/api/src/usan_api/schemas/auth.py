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
    # Deployed build version (git tag, e.g. "v0.12.0"; "dev" locally). The admin UI shows
    # it in the sidebar footer. Served here — not /health — because Caddy only proxies
    # /v1/* to the API on the admin origin, so the browser can't reach /health.
    version: str


class SwitchOrgRequest(BaseModel):
    organization_id: uuid.UUID
