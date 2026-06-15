"""Pure unit tests for schemas/custom_variables (spec §3.2).

Pins the create-side contracts — the snake-case slug pattern (mirror of the DB
CHECK ``ck_custom_variables_name_slug``), the builtin-collision rejection
(authority stays in code; the DB enforces only slug shape + uniqueness), the
description/example caps — and the PATCH shape: ``CustomVariableUpdate`` has NO
name field and forbids extras (name is immutable after create; a rename would
silently orphan ``{{tokens}}`` already saved in templates). No DB.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.db.models import CustomVariable
from usan_api.schemas.variable_catalog import BUILTIN_NAMES


def test_create_accepts_valid_slug():
    from usan_api.schemas.custom_variables import CustomVariableCreate

    explicit = CustomVariableCreate(name="pet_name", description="", example="", phi=False)
    assert explicit.name == "pet_name"

    defaults = CustomVariableCreate(name="pet_name")
    assert defaults.description == ""
    assert defaults.example == ""
    assert defaults.phi is False


@pytest.mark.parametrize(
    "bad_name",
    ["Bad", "9name", "has space", "has-dash", "a" * 65, ""],
    ids=["uppercase", "leading-digit", "space", "dash", "too-long", "empty"],
)
def test_create_rejects_bad_slugs(bad_name: str):
    from usan_api.schemas.custom_variables import CustomVariableCreate

    with pytest.raises(ValidationError):
        CustomVariableCreate(name=bad_name)


@pytest.mark.parametrize("builtin", sorted(BUILTIN_NAMES))
def test_create_rejects_builtin_collision(builtin: str):
    from usan_api.schemas.custom_variables import CustomVariableCreate

    # All 10 frozen builtins collide; the message names the builtin tier
    # (authority stays in code, spec §3.2 — the DB knows nothing of builtins).
    with pytest.raises(ValidationError, match="builtin"):
        CustomVariableCreate(name=builtin)


def test_create_caps_description_and_example():
    from usan_api.schemas.custom_variables import CustomVariableCreate

    with pytest.raises(ValidationError):
        CustomVariableCreate(name="ok_name", description="d" * 501)
    with pytest.raises(ValidationError):
        CustomVariableCreate(name="ok_name", example="e" * 201)
    # At-cap lengths are fine.
    at_cap = CustomVariableCreate(name="ok_name", description="d" * 500, example="e" * 200)
    assert len(at_cap.description) == 500
    assert len(at_cap.example) == 200


def test_update_has_no_name_field_and_forbids_extras():
    from usan_api.schemas.custom_variables import CustomVariableUpdate

    # Immutability by construction: no name field exists on the PATCH shape.
    assert "name" not in CustomVariableUpdate.model_fields
    # extra="forbid" — the 422-on-name-change-attempt contract.
    with pytest.raises(ValidationError):
        CustomVariableUpdate.model_validate({"name": "x"})
    # Empty body validates (all-optional partial update).
    empty = CustomVariableUpdate.model_validate({})
    assert empty.description is None
    assert empty.example is None
    assert empty.phi is None


def test_out_from_model_echoes_all_fields():
    from usan_api.schemas.custom_variables import CustomVariableOut

    now = datetime.now(UTC)
    row = CustomVariable(
        id=uuid.uuid4(),
        name="pet_name",
        description="The contact's pet's name.",
        example="Rex",
        phi=True,
        created_at=now,
        updated_at=now,
    )
    out = CustomVariableOut.from_model(row)
    assert out.id == row.id
    assert out.name == "pet_name"
    assert out.description == row.description
    assert out.example == "Rex"
    assert out.phi is True
    assert out.created_at == now
    assert out.updated_at == now
