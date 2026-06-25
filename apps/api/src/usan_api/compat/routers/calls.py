"""RetellAI-compatible call endpoints (feature 003, US1):

  POST /v2/create-phone-call   (201)
  GET  /v2/get-call/{id}
  POST /v2/stop-call/{id}      (204)
  POST /v2/update-call/{id}
  POST /v3/list-calls

Auth + org-scoped RLS session via ``get_compat_db``. Every operation emits a PHI-free audit
line (org id + operation + call id; never the token/PHI) — Constitution VI / FR-055.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_dispatch
from usan_api.client_ip import client_ip
from usan_api.compat import call_create, call_serializer, ids, status_map
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.calls import (
    CompatCall,
    CreatePhoneCallRequest,
    ListCallsRequest,
    ListCallsResponse,
    UpdateCallRequest,
)
from usan_api.compat.serialization import pack_dynamic_vars, unpack_dynamic_vars
from usan_api.db.base import CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-calls"])


def _audit(request: Request, op: str, call_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, call_id=call_id).info("compat call op={op}")


def _org_id(request: Request) -> uuid.UUID:
    # get_compat_db stashed it before the handler ran; always present here.
    return uuid.UUID(request.state.compat_org_id)


async def _load_call(db: AsyncSession, call_id: str) -> Call:
    call = await calls_repo.get_call(db, ids.decode_call_id(call_id))
    if call is None:
        raise CompatError(404, "call not found")
    return call


@router.post(
    "/v2/create-phone-call",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatCall,
    response_model_exclude_none=True,
)
async def create_phone_call(
    body: CreatePhoneCallRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatCall:
    call = await call_create.create_compat_call(
        db, settings, body, response, organization_id=_org_id(request)
    )
    response.status_code = status.HTTP_201_CREATED
    _audit(request, "create-phone-call", ids.encode_call_id(call.id))
    return await call_serializer.serialize_call(db, call, settings, client_host=client_ip(request))


@router.get("/v2/get-call/{call_id}", response_model=CompatCall, response_model_exclude_none=True)
async def get_call(
    call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatCall:
    call = await _load_call(db, call_id)
    _audit(request, "get-call", call_id)
    return await call_serializer.serialize_call(db, call, settings, client_host=client_ip(request))


@router.post("/v2/stop-call/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_call(
    call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    call = await _load_call(db, call_id)
    # Best-effort cancel: mark a not-yet-terminal call CANCELLED so the dialer/poller never
    # (re)dials it. Force-hang-up any live LiveKit room first (best-effort, never raises).
    if not status_map.is_terminal(call.status):
        if call.livekit_room:
            await livekit_dispatch.force_hangup(call.livekit_room, settings)
        await calls_repo.set_status(db, call.id, CallStatus.CANCELLED)
        await db.commit()
    _audit(request, "stop-call", call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# PATCH is the documented method; POST is accepted too (some clients send it) but hidden
# from the OpenAPI so the two methods don't collide on one auto-generated operation id.
@router.patch(
    "/v2/update-call/{call_id}", response_model=CompatCall, response_model_exclude_none=True
)
@router.post(
    "/v2/update-call/{call_id}",
    response_model=CompatCall,
    include_in_schema=False,
    response_model_exclude_none=True,
)
async def update_call(
    call_id: str,
    body: UpdateCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatCall:
    call = await _load_call(db, call_id)
    dynamic_variables, metadata = unpack_dynamic_vars(call.dynamic_vars)
    new_vars = body.override_dynamic_variables or body.retell_llm_dynamic_variables
    if new_vars is not None:
        dynamic_variables = new_vars
    if body.metadata is not None:
        metadata = body.metadata
    call.dynamic_vars = pack_dynamic_vars(dynamic_variables, metadata)
    await db.commit()
    await db.refresh(call)
    _audit(request, "update-call", call_id)
    return await call_serializer.serialize_call(db, call, settings, client_host=client_ip(request))


async def _query_calls(db: AsyncSession, body: ListCallsRequest) -> list[Call]:
    """Filter + keyset-paginate the org's calls (RLS-scoped). Filtering is intentionally
    minimal in the MVP (agent_id); unknown filter_criteria keys are silently ignored —
    FROZEN (oracle): pinned by test_list_calls_filter_ignores_unknown_keys."""
    stmt = select(Call)
    fc = body.filter_criteria or {}
    agent = fc.get("agent_id")
    if isinstance(agent, str) and agent:
        try:
            stmt = stmt.where(Call.profile_override == ids.decode_agent_id(agent))
        except CompatError:
            return []  # an unparseable agent_id matches nothing
    descending = body.sort_order != "ascending"
    if body.pagination_key:
        try:
            cursor = await calls_repo.get_call(db, ids.decode_call_id(body.pagination_key))
        except CompatError:
            cursor = None
        if cursor is not None:
            # Composite (created_at, id) keyset so calls sharing the cursor's exact timestamp
            # are never silently dropped across pages (id is the tiebreaker in BOTH the WHERE
            # and the ORDER BY).
            if descending:
                stmt = stmt.where(
                    or_(
                        Call.created_at < cursor.created_at,
                        and_(Call.created_at == cursor.created_at, Call.id < cursor.id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        Call.created_at > cursor.created_at,
                        and_(Call.created_at == cursor.created_at, Call.id > cursor.id),
                    )
                )
    if descending:
        stmt = stmt.order_by(Call.created_at.desc(), Call.id.desc())
    else:
        stmt = stmt.order_by(Call.created_at.asc(), Call.id.asc())
    if body.skip:
        stmt = stmt.offset(body.skip)
    stmt = stmt.limit(body.limit)
    return list((await db.execute(stmt)).scalars().all())


async def _count_calls(db: AsyncSession, body: ListCallsRequest) -> int:
    stmt = select(func.count()).select_from(Call)
    fc = body.filter_criteria or {}
    agent = fc.get("agent_id")
    if isinstance(agent, str) and agent:
        try:
            stmt = stmt.where(Call.profile_override == ids.decode_agent_id(agent))
        except CompatError:
            return 0
    return int((await db.execute(stmt)).scalar_one())


@router.post("/v3/list-calls", response_model=ListCallsResponse, response_model_exclude_none=True)
async def list_calls(
    body: ListCallsRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> ListCallsResponse:
    calls = await _query_calls(db, body)
    _audit(request, "list-calls")
    host = client_ip(request)
    # Lighter per-row serialization (no transcript/recording) to avoid N IAM signings;
    # full fidelity is available via get-call.
    items = [
        await call_serializer.serialize_call(
            db, c, settings, client_host=host, include_transcript=False, include_recording=False
        )
        for c in calls
    ]
    return ListCallsResponse(
        items=items,
        pagination_key=ids.encode_call_id(calls[-1].id) if calls else None,
        has_more=len(calls) == body.limit,
        total=await _count_calls(db, body) if body.include_total else None,
    )
