"""Request/response schemas for the custom variable catalog (spec §3.2).

``CustomVariableCreate`` is the code-side authority for the builtin-collision
rule: names colliding with the 10 frozen ``BUILTIN_NAMES`` are rejected here
(the DB CHECK enforces only slug shape + uniqueness). Definitions are
documentation/UX only — they carry NO values; values arrive per call via
``Call.dynamic_vars``. ``name`` is immutable after create (a rename would
silently orphan ``{{tokens}}`` already saved in templates; delete + recreate
instead): ``CustomVariableUpdate`` has no name field, and ``extra="forbid"``
turns a PATCH name-change attempt into a 422.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from usan_api.db.models import CustomVariable
from usan_api.schemas.variable_catalog import BUILTIN_NAMES

# Mirrors the DB CHECK ck_custom_variables_name_slug (migration 0015).
NAME_SLUG_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
# Catalog/UX copy caps. Definitions carry no values — descriptions and examples
# are operator-authored documentation, PHI-free by convention.
MAX_DESCRIPTION_LENGTH = 500
MAX_EXAMPLE_LENGTH = 200


class CustomVariableCreate(BaseModel):
    """POST body — name is fixed here forever (immutable after create)."""

    name: str = Field(pattern=NAME_SLUG_PATTERN)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LENGTH)
    example: str = Field(default="", max_length=MAX_EXAMPLE_LENGTH)
    phi: bool = False

    @field_validator("name")
    @classmethod
    def _reject_builtin_collision(cls, v: str) -> str:
        # Authority stays in code (spec §3.2): the DB knows nothing of builtins,
        # so the create validator is the only gate against shadowing them.
        if v in BUILTIN_NAMES:
            raise ValueError(
                f"name '{v}' collides with the builtin variable tier; builtin names are reserved"
            )
        return v


class CustomVariableUpdate(BaseModel):
    """PATCH body — description/example/phi only; ``name`` deliberately absent.

    ``extra="forbid"`` makes a name-change attempt a 422 (immutable after
    create, spec §4). Present fields run the same caps as create.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    example: str | None = Field(default=None, max_length=MAX_EXAMPLE_LENGTH)
    phi: bool | None = None


class CustomVariableOut(BaseModel):
    """Read shape for list/create/PATCH responses — full row echo, no values."""

    id: uuid.UUID
    name: str
    description: str
    example: str
    phi: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, v: CustomVariable) -> CustomVariableOut:
        return cls(
            id=v.id,
            name=v.name,
            description=v.description,
            example=v.example,
            phi=v.phi,
            created_at=v.created_at,
            updated_at=v.updated_at,
        )


class VariableReference(BaseModel):
    """One profile that references a custom variable's ``{{name}}`` token.

    ``where`` lists the locations as ``"<source>:<field>"`` — source is ``draft``
    or ``v<N>`` (a published version), field is a prompt field name or
    ``sms[<key>]``. Names + locations only, never prompt text or per-call values
    (spec §7).
    """

    id: uuid.UUID
    name: str
    where: list[str]


class CustomVariableReferences(BaseModel):
    """Delete-guard payload (FR-007): the profiles still referencing the variable."""

    profiles: list[VariableReference]
