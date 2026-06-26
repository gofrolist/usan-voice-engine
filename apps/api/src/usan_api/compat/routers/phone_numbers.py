"""RetellAI-compat phone-number surface (Phase 2): import/get/update/list/delete.

Agent bindings (inbound/outbound[/_sms]_agents) are PERSISTED and echoed but NOT yet honored
at call-routing time — the runtime call-plane is single-org and outbound dial uses the global
caller-id. See docs/deployment/phone-numbers-bindings-deferred.md. create-phone-number stays a
documented 501 (Telnyx DID purchase unavailable).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.phone_numbers import (
    ImportPhoneNumberRequest,
    PhoneNumberResponse,
    UpdatePhoneNumberRequest,
    serialize_phone_number,
)
from usan_api.db.base import ProfileStatus
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import phone_numbers as phones_repo

router = APIRouter(tags=["compat-phone-numbers"])

# E.164: leading +, country digit 1-9, up to 14 more digits.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

# update-request field -> ORM column (the auth_* vs sip_auth_* divergence).
_UPDATE_COLUMN_MAP = {"auth_username": "sip_auth_username", "auth_password": "sip_auth_password"}


def _audit(request: Request, op: str) -> None:
    # PHI-free: org + op only. NEVER the E.164 (it is the path param, masked in access logs)
    # and NEVER any sip auth secret.
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat phone op={op}")


async def _resolve_binding_agents(db: AsyncSession, *lists: list[Any] | None) -> None:
    """Validate every AgentWeight.agent_id resolves to a non-archived org AgentProfile -> 422.

    decode_agent_id already raises CompatError(422) on a malformed id; an unknown/archived
    (well-formed) id is treated as a body-validation failure (422), not a 404 path-not-found.
    """
    for agents in lists:
        if not agents:
            continue
        for aw in agents:
            profile_id = ids.decode_agent_id(aw.agent_id)  # CompatError(422) on malformed
            profile = await profiles_repo.get_profile(db, profile_id)
            if profile is None or profile.status == ProfileStatus.ARCHIVED:
                raise CompatError(422, f"invalid request: unknown agent_id {aw.agent_id}")


def _binding_dicts(agents: list[Any] | None) -> list[dict[str, Any]] | None:
    return [aw.model_dump(exclude_none=True) for aw in agents] if agents else None


@router.post(
    "/import-phone-number",
    status_code=status.HTTP_201_CREATED,
    response_model=PhoneNumberResponse,
    response_model_exclude_none=True,
)
async def import_phone_number(
    body: ImportPhoneNumberRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> PhoneNumberResponse:
    if not body.ignore_e164_validation and not _E164_RE.match(body.phone_number):
        raise CompatError(400, "invalid E.164 phone number")
    await _resolve_binding_agents(db, body.inbound_agents, body.outbound_agents)
    if await phones_repo.get_by_e164(db, body.phone_number) is not None:
        raise CompatError(400, "phone number already imported")
    try:
        pn = await phones_repo.create_phone_number(
            db,
            phone_e164=body.phone_number,
            phone_number_type="custom",  # BYO SIP number
            nickname=body.nickname,
            inbound_webhook_url=body.inbound_webhook_url,
            allowed_inbound_country_list=body.allowed_inbound_country_list,
            allowed_outbound_country_list=body.allowed_outbound_country_list,
            transport=body.transport,
            termination_uri=body.termination_uri,
            sip_auth_username=body.sip_trunk_auth_username,
            sip_auth_password=body.sip_trunk_auth_password,
            inbound_agents=_binding_dicts(body.inbound_agents),
            outbound_agents=_binding_dicts(body.outbound_agents),
        )
    except IntegrityError as exc:
        await db.rollback()
        raise CompatError(400, "phone number already imported") from exc
    await db.commit()
    _audit(request, "import-phone-number")
    return serialize_phone_number(pn)


@router.get(
    "/get-phone-number/{phone_number}",
    response_model=PhoneNumberResponse,
    response_model_exclude_none=True,
)
async def get_phone_number(
    phone_number: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> PhoneNumberResponse:
    pn = await phones_repo.get_by_e164(db, phone_number)
    if pn is None:
        raise CompatError(404, "phone number not found")
    _audit(request, "get-phone-number")
    return serialize_phone_number(pn)


@router.delete("/delete-phone-number/{phone_number}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phone_number(
    phone_number: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    if not await phones_repo.delete_by_e164(db, phone_number):
        raise CompatError(404, "phone number not found")
    await db.commit()
    _audit(request, "delete-phone-number")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/update-phone-number/{phone_number}",
    response_model=PhoneNumberResponse,
    response_model_exclude_none=True,
)
async def update_phone_number(
    phone_number: str,
    body: UpdatePhoneNumberRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> PhoneNumberResponse:
    await _resolve_binding_agents(
        db,
        body.inbound_agents,
        body.outbound_agents,
        body.inbound_sms_agents,
        body.outbound_sms_agents,
    )
    # exclude_unset: only the fields the client sent (an explicit null clears that column).
    provided = body.model_dump(exclude_unset=True)
    fields: dict[str, Any] = {}
    for key, value in provided.items():
        if key in (
            "inbound_agents",
            "outbound_agents",
            "inbound_sms_agents",
            "outbound_sms_agents",
        ):
            fields[key] = value  # already list[dict] | None from model_dump
        else:
            fields[_UPDATE_COLUMN_MAP.get(key, key)] = value  # used by update_phone_number
    pn = await phones_repo.update_by_e164(db, phone_number, fields)
    if pn is None:
        raise CompatError(404, "phone number not found")
    await db.commit()
    _audit(request, "update-phone-number")
    return serialize_phone_number(pn)


@router.get("/v2/list-phone-numbers")
async def list_phone_numbers(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    after_id = None
    if pagination_key:
        import contextlib

        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after_id = ids.decode_phone_number_cursor(pagination_key)
    rows = await phones_repo.list_phone_numbers(
        db, limit=limit, descending=(sort_order != "ascending"), after_id=after_id
    )
    _audit(request, "list-phone-numbers")
    items = [serialize_phone_number(p).model_dump(exclude_none=True) for p in rows]
    out: dict[str, Any] = {"items": items, "has_more": len(rows) == limit}
    if rows and len(rows) == limit:
        out["pagination_key"] = ids.encode_phone_number_cursor(rows[-1].id)
    return out
