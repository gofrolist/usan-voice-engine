from pydantic import BaseModel, Field

# Minimal email regex avoids adding the email-validator dependency that EmailStr needs.
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class MeResponse(BaseModel):
    email: str
    role: str


class AdminUserOut(BaseModel):
    email: str
    role: str
    added_by: str | None = None


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")
