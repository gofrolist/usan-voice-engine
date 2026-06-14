import asyncio

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.schemas.variable_catalog import BUILTIN_VARIABLES

# The builtins in canonical catalog/display order (design §3.1) — the merge
# keeps them first, before any customs (spec §3.2). contact_name (US4 / FR-024)
# is the elder_name alias, listed adjacent to it.
BUILTIN_ORDER = [
    "first_name",
    "elder_name",
    "contact_name",
    "call_direction",
    "current_time",
    "current_date",
    "last_check_in",
    "last_check_in_line",
    "last_mood",
    "last_pain",
    "today_meds",
]
_N_BUILTINS = len(BUILTIN_ORDER)


def test_variable_catalog_requires_admin_session(client):
    # Mirrors the admin-profiles plane: no session cookie -> 401.
    r = client.get("/v1/admin/variable-catalog")
    assert r.status_code == 401


def test_variable_catalog_returns_builtins_in_order(client, admin_session):
    r = client.get("/v1/admin/variable-catalog")
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["variables"]
    variables = body["variables"]
    assert [v["name"] for v in variables] == BUILTIN_ORDER


def test_variable_catalog_each_entry_has_contract_shape(client, admin_session):
    variables = client.get("/v1/admin/variable-catalog").json()["variables"]
    for v in variables:
        assert set(v.keys()) == {"name", "tier", "description", "default", "example", "phi"}
        assert v["tier"] == "builtin"
    by_name = {v["name"]: v for v in variables}
    assert by_name["first_name"]["default"] == "there"
    assert by_name["first_name"]["example"] == "Margaret"
    assert by_name["today_meds"]["default"] == ""


def test_variable_catalog_phi_field_values(client, admin_session):
    variables = client.get("/v1/admin/variable-catalog").json()["variables"]
    by_name = {v["name"]: v for v in variables}
    phi_names = {"last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"}
    for name, v in by_name.items():
        if name in phi_names:
            assert v["phi"] is True, f"{name} should have phi=True"
        else:
            assert v["phi"] is False, f"{name} should have phi=False"


async def _insert_custom_raw(async_database_url: str, name: str) -> None:
    """Raw-SQL insert bypassing the Pydantic builtin-collision validator.

    Simulates the *future-builtin* scenario (spec §3.2): a custom row created
    before its name joined BUILTIN_VARIABLES.
    """
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO custom_variables (name) VALUES (:n)"), {"n": name})
    finally:
        await engine.dispose()


def test_catalog_merges_customs_after_builtins(client, admin_session):
    # Customs declared via the CRUD API show up after the builtins, alphabetical,
    # with tier="custom" and default="" (definitions carry no values — spec §3.2).
    for name, phi in (("zebra_var", False), ("apple_var", True)):
        r = client.post(
            "/v1/admin/custom-variables",
            json={"name": name, "description": f"about {name}", "example": "ex", "phi": phi},
        )
        assert r.status_code == 201

    variables = client.get("/v1/admin/variable-catalog").json()["variables"]
    assert [v["name"] for v in variables[:_N_BUILTINS]] == BUILTIN_ORDER
    customs = variables[_N_BUILTINS:]
    assert [v["name"] for v in customs] == ["apple_var", "zebra_var"]
    for v in customs:
        assert v["tier"] == "custom"
        assert v["default"] == ""
        assert v["description"] == f"about {v['name']}"
        assert v["example"] == "ex"
    by_name = {v["name"]: v for v in customs}
    assert by_name["apple_var"]["phi"] is True
    assert by_name["zebra_var"]["phi"] is False


def test_catalog_empty_table_identical_to_builtin_constant(client, admin_session):
    # Ship-inert pin (spec §9): an empty custom_variables table reproduces the
    # pre-A4 static catalog byte-for-byte.
    body = client.get("/v1/admin/variable-catalog").json()
    assert body["variables"] == [v.model_dump(mode="json") for v in BUILTIN_VARIABLES]


def test_builtin_shadowed_custom_dropped_and_logged(client, admin_session, async_database_url):
    # Create-time validation rejects collisions with *today's* builtins, but a
    # future builtin can collide with a pre-existing custom row. The merge drops
    # the custom and warns with the name only (spec §3.2).
    asyncio.run(_insert_custom_raw(async_database_url, "elder_name"))

    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        variables = client.get("/v1/admin/variable-catalog").json()["variables"]
    finally:
        logger.remove(handler_id)

    matches = [v for v in variables if v["name"] == "elder_name"]
    assert len(matches) == 1
    assert matches[0]["tier"] == "builtin"

    shadow = [r for r in records if "shadowed by builtin" in r["message"]]
    assert len(shadow) == 1
    # Bound with the variable *name* only — never values (spec §7).
    assert shadow[0]["extra"]["name"] == "elder_name"
