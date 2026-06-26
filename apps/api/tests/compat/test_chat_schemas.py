from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.chats import (
    CompatChat,
    CreateChatCompletionRequest,
    CreateChatRequest,
    ListChatsRequest,
    UpdateChatRequest,
)


def test_create_chat_requires_agent_id():
    with pytest.raises(ValidationError):
        CreateChatRequest()  # type: ignore[call-arg]
    m = CreateChatRequest(agent_id="agent_x", retell_llm_dynamic_variables={"n": "p"})
    assert m.agent_id == "agent_x"


def test_create_chat_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        CreateChatRequest(agent_id="agent_x", bogus=1)  # type: ignore[call-arg]


def test_completion_requires_chat_id_and_content():
    with pytest.raises(ValidationError):
        CreateChatCompletionRequest(chat_id="chat_x")  # type: ignore[call-arg]
    assert CreateChatCompletionRequest(chat_id="chat_x", content="hi").content == "hi"


def test_update_chat_data_storage_setting_enum():
    UpdateChatRequest(data_storage_setting="everything")
    with pytest.raises(ValidationError):
        UpdateChatRequest(data_storage_setting="nonsense")


def test_list_chats_skip_xor_pagination_key():
    ListChatsRequest(skip=5)
    ListChatsRequest(pagination_key="chat_x")
    with pytest.raises(ValidationError):
        ListChatsRequest(skip=5, pagination_key="chat_x")


def test_compat_chat_omits_empty_optionals():
    c = CompatChat(chat_id="chat_x", agent_id="agent_y", chat_status="ongoing")
    dumped = c.model_dump(exclude_none=True)
    assert dumped == {
        "chat_id": "chat_x",
        "agent_id": "agent_y",
        "chat_status": "ongoing",
        "chat_type": "api_chat",
    }
