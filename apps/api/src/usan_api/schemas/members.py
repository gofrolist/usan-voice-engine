from pydantic import BaseModel, Field

# Minimal email regex avoids adding the email-validator dependency that EmailStr needs.
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class MemberOut(BaseModel):
    email: str
    role: str
    added_by: str | None = None


class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")


class MemberRoleUpdate(BaseModel):
    role: str = Field(pattern="^(admin|viewer)$")
