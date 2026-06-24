"""Repository for `custom_variables` rows (operator-declared prompt variables).

Definitions are documentation/UX only — values arrive per call via
``Call.dynamic_vars``, never through this table (spec §4). ``name`` is immutable
after create: ``update_custom_variable`` has no name parameter by construction
(a rename would silently orphan ``{{tokens}}`` already saved in templates).

House rules: functions take the request session, ``flush()`` (+``refresh()``
for returned rows), and never commit — routers own the transaction boundary.
"""

import re
import uuid
from collections.abc import Iterable
from typing import Any

from loguru import logger
from sqlalchemy import Text, cast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import AgentProfile, AgentProfileVersion, CustomVariable
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


# US4 (FR-024): contact_name became a builtin alias of contact_name. A custom row
# named contact_name declared BEFORE that change is now silently shadowed by the
# builtin (the catalog merge / _drop_builtin_shadowed drops it). This is a
# deploy-time guard so the operator is told once, by name only (spec §7).
_CONTACT_NAME = "contact_name"


async def warn_if_contact_name_custom_exists(db: AsyncSession) -> bool:
    """Log a name-only warning if a custom variable named ``contact_name`` exists.

    Run at startup (deploy time). The new ``contact_name`` builtin shadows any
    pre-existing custom row of the same name; the catalog merge already drops it
    with a logged warning, but operators won't see that unless prompted. Returns
    ``True`` when a row was found (and warned). Never raises on a missing row;
    the message carries only the name — never the row's description/example/values.
    """
    result = await db.execute(
        select(CustomVariable.name).where(CustomVariable.name == _CONTACT_NAME).limit(1)
    )
    if result.scalar_one_or_none() is None:
        return False
    logger.bind(name=_CONTACT_NAME).warning(
        "custom variable {name} is now shadowed by the builtin alias of contact_name; "
        "its definition is ignored — rename or remove it",
        name=_CONTACT_NAME,
    )
    return True


# The 8 prompt fields scanned for {{token}} references (mirrors agent_config
# PromptsConfig + the save-path warning scan). SMS template bodies are scanned too.
_PROMPT_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "greeting",
    "recording_disclosure",
    "voicemail_message",
    "checkin_flow_instructions",
    "goodbye_message",
    "inbound_opening",
    "inbound_personalization_template",
)


def _token_re(name: str) -> re.Pattern[str]:
    """Exact {{name}} matcher (mirrors agent_config._TOKEN_RE's inner-space rule).

    Built per-name with re.escape so a scan for ``state`` never matches
    ``{{state_full}}`` (exact token, not substring).
    """
    return re.compile(r"\{\{\s*" + re.escape(name) + r"\s*\}\}")


def _locations_in_config(config: dict[str, Any], pattern: re.Pattern[str]) -> list[str]:
    """Field identifiers within one AgentConfig dict whose text contains the token.

    Returns prompt field names (e.g. ``greeting``) and ``sms[<key>]`` for matching
    SMS template bodies. Names/locations only — never the prompt text itself.
    """
    locs: list[str] = []
    prompts = config.get("prompts") or {}
    for field in _PROMPT_FIELDS:
        text = prompts.get(field)
        if isinstance(text, str) and pattern.search(text):
            locs.append(field)
    templates = ((config.get("tools") or {}).get("sms") or {}).get("templates") or []
    for tmpl in templates:
        if not isinstance(tmpl, dict):
            continue
        body = tmpl.get("body")
        if isinstance(body, str) and pattern.search(body):
            locs.append(f"sms[{tmpl.get('key', '')}]")
    return locs


async def references_to(db: AsyncSession, name: str) -> list[dict[str, Any]]:
    """Profiles referencing ``{{name}}`` across the live draft AND every version.

    JSONB ``::text ILIKE '%{{name}}%'`` prefilters the rows to scan, then an exact
    ``_token_re`` confirm over the 8 prompt fields + SMS bodies avoids substring
    false positives. Returns ``[{id, name, where}]`` grouped per profile, where
    ``where`` items are ``"draft:<field>"`` / ``"v<N>:<field>"`` — names/locations
    only, never prompt text or per-call values (spec §7 / FR-007).
    """
    pattern = _token_re(name)
    # Escape LIKE metacharacters in `name` so an underscore (allowed by the variable
    # name schema) is matched literally rather than as a single-char wildcard, keeping
    # the prefilter set tight. The surrounding {{ }} are literal text, the outer % are
    # the intended wildcards. `_token_re` is the authoritative confirm regardless.
    safe = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = "%{{" + safe + "}}%"
    by_profile: dict[uuid.UUID, dict[str, Any]] = {}

    draft_rows = await db.execute(
        select(AgentProfile.id, AgentProfile.name, AgentProfile.draft_config).where(
            cast(AgentProfile.draft_config, Text).ilike(like, escape="\\")
        )
    )
    for pid, pname, cfg in draft_rows.all():
        locs = _locations_in_config(cfg or {}, pattern)
        if locs:
            entry = by_profile.setdefault(pid, {"id": pid, "name": pname, "where": []})
            entry["where"].extend(f"draft:{loc}" for loc in locs)

    version_rows = await db.execute(
        select(
            AgentProfileVersion.profile_id,
            AgentProfile.name,
            AgentProfileVersion.version,
            AgentProfileVersion.config,
        )
        .join(AgentProfile, AgentProfile.id == AgentProfileVersion.profile_id)
        .where(cast(AgentProfileVersion.config, Text).ilike(like, escape="\\"))
        .order_by(AgentProfileVersion.version)
    )
    for pid, pname, version, cfg in version_rows.all():
        locs = _locations_in_config(cfg or {}, pattern)
        if locs:
            entry = by_profile.setdefault(pid, {"id": pid, "name": pname, "where": []})
            entry["where"].extend(f"v{version}:{loc}" for loc in locs)

    return list(by_profile.values())
