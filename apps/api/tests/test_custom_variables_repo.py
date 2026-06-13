"""custom_variables repository: CRUD roundtrip, duplicate domain error, catalog helpers."""

import pytest
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import custom_variables as custom_variables_repo
from usan_api.repositories.custom_variables import DuplicateCustomVariableError


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE custom_variables"))
        await db.commit()


async def test_create_list_get_update_delete_roundtrip(session_factory) -> None:
    async with session_factory() as db:
        zebra = await custom_variables_repo.create_custom_variable(
            db, name="zebra_var", description="z desc", example="z ex", phi=False
        )
        apple = await custom_variables_repo.create_custom_variable(
            db, name="apple_var", description="a desc", example="a ex", phi=False
        )
        await db.commit()

    async with session_factory() as db:
        rows = await custom_variables_repo.list_custom_variables(db)
        assert [r.name for r in rows] == ["apple_var", "zebra_var"]  # alphabetical

        fetched = await custom_variables_repo.get_custom_variable(db, zebra.id)
        assert fetched is not None
        assert fetched.name == "zebra_var"
        assert fetched.description == "z desc"
        assert fetched.example == "z ex"
        assert fetched.phi is False

        updated = await custom_variables_repo.update_custom_variable(
            db, fetched, description="z desc 2", example="z ex 2", phi=True
        )
        await db.commit()
        assert updated.description == "z desc 2"
        assert updated.example == "z ex 2"
        assert updated.phi is True
        assert updated.name == "zebra_var"  # name immutable — no parameter exists
        assert updated.id == zebra.id

    async with session_factory() as db:
        row = await custom_variables_repo.get_custom_variable(db, apple.id)
        assert row is not None
        await custom_variables_repo.delete_custom_variable(db, row)
        row2 = await custom_variables_repo.get_custom_variable(db, zebra.id)
        assert row2 is not None
        await custom_variables_repo.delete_custom_variable(db, row2)
        await db.commit()

    async with session_factory() as db:
        assert await custom_variables_repo.list_custom_variables(db) == []


async def test_create_duplicate_raises_domain_error(session_factory) -> None:
    async with session_factory() as db:
        await custom_variables_repo.create_custom_variable(
            db, name="pet_name", description="", example="", phi=False
        )
        with pytest.raises(DuplicateCustomVariableError):
            await custom_variables_repo.create_custom_variable(
                db, name="pet_name", description="other", example="", phi=True
            )
        # SAVEPOINT-wrapped flush: the session stays usable after the duplicate.
        rows = await custom_variables_repo.list_custom_variables(db)
        assert [r.name for r in rows] == ["pet_name"]
        await db.commit()


async def test_names_and_phi_names_helpers(session_factory) -> None:
    async with session_factory() as db:
        assert await custom_variables_repo.names(db) == frozenset()
        assert await custom_variables_repo.phi_names(db) == frozenset()

    async with session_factory() as db:
        await custom_variables_repo.create_custom_variable(
            db, name="pet_name", description="", example="", phi=False
        )
        await custom_variables_repo.create_custom_variable(
            db, name="diagnosis", description="", example="", phi=True
        )
        await db.commit()

    async with session_factory() as db:
        assert await custom_variables_repo.names(db) == frozenset({"pet_name", "diagnosis"})
        assert await custom_variables_repo.phi_names(db) == frozenset({"diagnosis"})


async def test_names_helpers_exclude_builtin_shadowed_rows(session_factory) -> None:
    # Shadowing consistency with the catalog merge (spec §3.2): a custom row can
    # collide with a builtin added AFTER its creation (create-time validation
    # only knows the builtins of its day). The catalog DROPS such rows, so the
    # enforcement fetches must too — otherwise a shadowed phi=true row keeps
    # 422-blocking SMS bodies that reference the (non-PHI) builtin name the
    # operator actually sees in the catalog. Same logged drop as the merge.
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        async with session_factory() as db:
            # Repo-level insert bypasses the pydantic builtin-collision gate,
            # exactly like a pre-existing row a future builtin later shadows.
            await custom_variables_repo.create_custom_variable(
                db, name="elder_name", description="", example="", phi=True
            )
            await custom_variables_repo.create_custom_variable(
                db, name="diagnosis", description="", example="", phi=True
            )
            await db.commit()

        async with session_factory() as db:
            assert await custom_variables_repo.names(db) == frozenset({"diagnosis"})
            assert await custom_variables_repo.phi_names(db) == frozenset({"diagnosis"})
    finally:
        logger.remove(handler_id)
    shadowed = [r for r in records if "shadowed by builtin" in r["message"]]
    assert shadowed, "expected the logged drop, mirroring the catalog merge"
    assert all(r["extra"].get("name") == "elder_name" for r in shadowed)


# ---------------------------------------------------------------------------
# US4 (T045) — deploy-time guard: warn if a pre-existing custom `contact_name`
# row exists so the new builtin alias doesn't silently shadow it.
# ---------------------------------------------------------------------------


async def test_contact_name_deploy_check_warns_when_custom_row_exists(session_factory) -> None:
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        async with session_factory() as db:
            # Repo-level insert bypasses the pydantic builtin-collision gate, exactly
            # like a row declared before the contact_name builtin was introduced.
            await custom_variables_repo.create_custom_variable(
                db, name="contact_name", description="legacy", example="", phi=True
            )
            await db.commit()

        async with session_factory() as db:
            shadowed = await custom_variables_repo.warn_if_contact_name_custom_exists(db)
        assert shadowed is True
    finally:
        logger.remove(handler_id)
    hits = [r for r in records if "contact_name" in r["message"]]
    assert hits, "expected a name-only warning about the shadowed contact_name custom"
    # Name-only: the warning must not carry the row's description/example/values.
    assert all("legacy" not in r["message"] for r in hits)


async def test_contact_name_deploy_check_silent_when_absent(session_factory) -> None:
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        async with session_factory() as db:
            await custom_variables_repo.create_custom_variable(
                db, name="pet_name", description="", example="", phi=False
            )
            await db.commit()
        async with session_factory() as db:
            shadowed = await custom_variables_repo.warn_if_contact_name_custom_exists(db)
        assert shadowed is False
    finally:
        logger.remove(handler_id)
    assert not [r for r in records if "contact_name" in r["message"]]
