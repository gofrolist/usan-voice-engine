"""Repository for `custom_variables` rows (operator-declared prompt variables).

Definitions are documentation/UX only — values arrive per call via
``Call.dynamic_vars``, never through this table (spec §4). ``name`` is immutable
after create: ``update_custom_variable`` has no name parameter by construction
(a rename would silently orphan ``{{tokens}}`` already saved in templates).

House rules: functions take the request session, ``flush()`` (+``refresh()``
for returned rows), and never commit — routers own the transaction boundary.
"""

import uuid
from collections.abc import Iterable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CustomVariable
from usan_api.schemas.variable_catalog import BUILTIN_NAMES


class DuplicateCustomVariableError(Exception):
    """A custom variable with this name already exists.

    The message is user-facing: the C3 router returns it in the 409 body. It
    carries the variable *name* only — never per-call values (spec §7).
    """


async def create_custom_variable(
    db: AsyncSession, *, name: str, description: str, example: str, phi: bool
) -> CustomVariable:
    """Insert a definition. Raises DuplicateCustomVariableError on a taken name.

    The flush is SAVEPOINT-wrapped (``begin_nested``) so the duplicate rolls
    back here only and the session stays usable for the caller's error path.
    Slug shape and builtin-collision are enforced upstream in the Pydantic
    layer; the DB CHECK is a backstop and would also surface as IntegrityError.
    """
    row = CustomVariable(name=name, description=description, example=example, phi=phi)
    try:
        async with db.begin_nested():  # SAVEPOINT: a duplicate rolls back here only
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise DuplicateCustomVariableError(f"custom variable '{name}' already exists") from exc
    await db.refresh(row)
    return row


async def get_custom_variable(db: AsyncSession, variable_id: uuid.UUID) -> CustomVariable | None:
    return await db.get(CustomVariable, variable_id)


async def list_custom_variables(db: AsyncSession) -> list[CustomVariable]:
    """All definitions, alphabetical by name (the catalog merge order, spec §3.2)."""
    result = await db.execute(select(CustomVariable).order_by(CustomVariable.name))
    return list(result.scalars().all())


async def update_custom_variable(
    db: AsyncSession,
    row: CustomVariable,
    *,
    description: str | None = None,
    example: str | None = None,
    phi: bool | None = None,
) -> CustomVariable:
    """Apply the present fields only. ``name`` is immutable — no parameter exists."""
    if description is not None:
        row.description = description
    if example is not None:
        row.example = example
    if phi is not None:
        row.phi = phi
    await db.flush()
    await db.refresh(row)
    return row


async def delete_custom_variable(db: AsyncSession, row: CustomVariable) -> None:
    """Hard delete — no referential scan against profile configs: tokens that
    referenced the name revert to unknown-token warnings (spec §4)."""
    await db.delete(row)
    await db.flush()


def _drop_builtin_shadowed(fetched: Iterable[str]) -> frozenset[str]:
    """Drop names shadowed by a builtin, with the catalog merge's logged drop.

    Shadowing consistency (spec §3.2): a future builtin can collide with a
    pre-existing custom row; the catalog merge drops it, so the enforcement
    fetches must agree — a shadowed row's definition (incl. its ``phi`` flag)
    is invisible to operators and must not keep gating saves. Name only in the
    log — never values (spec §7).
    """
    kept: list[str] = []
    for name in fetched:
        if name in BUILTIN_NAMES:
            logger.bind(name=name).warning(
                "custom variable {name} shadowed by builtin; ignored", name=name
            )
            continue
        kept.append(name)
    return frozenset(kept)


async def names(db: AsyncSession) -> frozenset[str]:
    """Declared, non-builtin-shadowed names (the save-path fetch, spec §3.2)."""
    result = await db.execute(select(CustomVariable.name))
    return _drop_builtin_shadowed(result.scalars().all())


async def phi_names(db: AsyncSession) -> frozenset[str]:
    """Non-builtin-shadowed names declared phi=true (the save-path fetch, spec §3.2)."""
    result = await db.execute(select(CustomVariable.name).where(CustomVariable.phi.is_(True)))
    return _drop_builtin_shadowed(result.scalars().all())
