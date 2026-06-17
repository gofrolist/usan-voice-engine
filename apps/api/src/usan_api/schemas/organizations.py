import uuid

from pydantic import BaseModel, Field

_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern="^[a-z0-9-]{2,40}$")
    first_admin_email: str | None = Field(default=None, pattern=_EMAIL)
