"""RetellAI-compatible chat (api_chat) endpoints (Phase 4a):

  POST   /create-chat              (201)
  POST   /create-chat-completion   (201)
  GET    /get-chat/{chat_id}
  POST   /v3/list-chats
  PATCH  /update-chat/{chat_id}
  PATCH  /end-chat/{chat_id}        (204)
  DELETE /delete-chat/{chat_id}     (204)

Auth + org-scoped RLS via get_compat_db. Each op emits a PHI-free audit line.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import chat_serializer, chat_service, ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.schemas.chats import (
    CompatChat,
    CompatChatCompletion,
    CompatChatMessage,
    CreateChatCompletionRequest,
    CreateChatRequest,
    CreateSmsChatRequest,
    ListChatsRequest,
    ListChatsResponse,
    UpdateChatRequest,
)
from usan_api.compat.serialization import to_ms
from usan_api.db.models import ChatSession
from usan_api.repositories import chat_analyses as chat_analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-chats"])


def _audit(request: Request, op: str, chat_id: str | None = None) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op, chat_id=chat_id).info("compat chat op={op}")


async def _serialize_full(db: AsyncSession, session: ChatSession) -> CompatChat:
    messages = await chats_repo.list_messages(db, session.id)
    analysis = await chat_analyses_repo.get_for_session(db, session.id)
    return chat_serializer.serialize_chat(
        session, messages, include_transcript=True, analysis=analysis
    )


@router.post(
    "/create-chat",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatChat,
    response_model_exclude_none=True,
)
async def create_chat(
    body: CreateChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> CompatChat:
    session = await chat_service.create_chat(db, body)
    _audit(request, "create-chat", ids.encode_chat_id(session.id))
    return await _serialize_full(db, session)


@router.post(
    "/create-sms-chat",
    status_code=status.HTTP_200_OK,
    response_model=CompatChat,
    response_model_exclude_none=True,
)
async def create_sms_chat(
    body: CreateSmsChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatChat:
    session = await chat_service.create_sms_chat(db, settings, body)
    _audit(request, "create-sms-chat", ids.encode_chat_id(session.id))
    return await _serialize_full(db, session)


@router.post(
    "/create-chat-completion",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatChatCompletion,
    response_model_exclude_none=True,
)
async def create_chat_completion(
    body: CreateChatCompletionRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatChatCompletion:
    new_messages = await chat_service.create_chat_completion(db, settings, body)
    _audit(request, "create-chat-completion", body.chat_id)
    return CompatChatCompletion(
        messages=[
            CompatChatMessage(
                role=m.role,
                content=m.content,
                message_id=ids.encode_message_id(m.id),
                created_timestamp=to_ms(m.created_at) or 0,
            )
            for m in new_messages
        ]
    )


@router.get("/get-chat/{chat_id}", response_model=CompatChat, response_model_exclude_none=True)
async def get_chat(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> CompatChat:
    session = await chat_service.get_chat(db, chat_id)
    _audit(request, "get-chat", chat_id)
    return await _serialize_full(db, session)


@router.put(
    "/rerun-chat-analysis/{chat_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=CompatChat,
    response_model_exclude_none=True,
)
async def rerun_chat_analysis(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> CompatChat:
    session = await chat_service.rerun_chat_analysis(db, settings, chat_id)
    _audit(request, "rerun-chat-analysis", chat_id)
    return await _serialize_full(db, session)


@router.post("/v3/list-chats", response_model=ListChatsResponse, response_model_exclude_none=True)
async def list_chats(
    body: ListChatsRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> ListChatsResponse:
    sessions, pagination_key, has_more, total = await chat_service.list_chats(db, body)
    _audit(request, "list-chats")
    analyses = await chat_analyses_repo.get_for_sessions(db, [s.id for s in sessions])
    items = [
        chat_serializer.serialize_chat(s, [], include_transcript=False, analysis=analyses.get(s.id))
        for s in sessions
    ]
    return ListChatsResponse(
        items=items, pagination_key=pagination_key, has_more=has_more, total=total
    )


@router.patch("/update-chat/{chat_id}", response_model=CompatChat, response_model_exclude_none=True)
async def update_chat(
    chat_id: str,
    body: UpdateChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> CompatChat:
    session = await chat_service.update_chat(db, chat_id, body)
    _audit(request, "update-chat", chat_id)
    return await _serialize_full(db, session)


@router.patch("/end-chat/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_chat(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await chat_service.end_chat(db, chat_id)
    _audit(request, "end-chat", chat_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/delete-chat/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    await chat_service.delete_chat(db, chat_id)
    _audit(request, "delete-chat", chat_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
