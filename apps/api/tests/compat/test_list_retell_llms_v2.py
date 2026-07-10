"""Phase 7 slice 2: GET /v2/list-retell-llms — keyset cursor codec + paginated list."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from usan_api.compat import ids
from usan_api.compat.errors import CompatError


def test_cursor_roundtrip():
    now = datetime.now(UTC)
    pid = uuid.uuid4()
    token = ids.encode_retell_llm_cursor(now, pid)
    assert ids.decode_retell_llm_cursor(token) == (now, pid)


def test_bad_cursor_raises_422():
    with pytest.raises(CompatError) as exc:
        ids.decode_retell_llm_cursor("not-a-cursor")
    assert exc.value.status_code == 422


def _make_llm(compat_client, compat_headers, prompt: str) -> str:
    r = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": prompt},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["llm_id"]


@pytest.mark.frozen
def test_v2_list_conforms_and_roundtrips(compat_client, compat_headers):
    for i in range(3):
        _make_llm(compat_client, compat_headers, f"prompt {i}")
    r = compat_client.get("/v2/list-retell-llms?limit=2", headers=compat_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_more"] is True
    assert isinstance(body["pagination_key"], str)
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert_conforms(item, "RetellLLMResponse")
    assert_sdk_roundtrip(body, "retell.types:LlmListResponse")


@pytest.mark.frozen
def test_v2_list_last_page_omits_pagination_key(compat_client, compat_headers):
    _make_llm(compat_client, compat_headers, "solo")
    body = compat_client.get("/v2/list-retell-llms", headers=compat_headers).json()
    assert body["has_more"] is False
    assert "pagination_key" not in body  # RetellAI omit-nulls


def test_v2_list_keyset_walk_no_duplicates(compat_client, compat_headers):
    made = {_make_llm(compat_client, compat_headers, f"p{i}") for i in range(5)}
    seen: list[str] = []
    key: str | None = None
    for _ in range(10):
        url = "/v2/list-retell-llms?limit=2" + (f"&pagination_key={key}" if key else "")
        body = compat_client.get(url, headers=compat_headers).json()
        seen.extend(item["llm_id"] for item in body["items"])
        if not body["has_more"]:
            break
        key = body["pagination_key"]
    assert len(seen) == len(set(seen))
    assert made <= set(seen)


def test_v2_list_ascending_is_reverse_of_descending(compat_client, compat_headers):
    for i in range(3):
        _make_llm(compat_client, compat_headers, f"s{i}")
    desc = compat_client.get("/v2/list-retell-llms?limit=1000", headers=compat_headers).json()
    asc = compat_client.get(
        "/v2/list-retell-llms?limit=1000&sort_order=ascending", headers=compat_headers
    ).json()
    assert [i["llm_id"] for i in asc["items"]] == [i["llm_id"] for i in desc["items"]][::-1]


def test_v2_list_bad_cursor_falls_back_to_first_page(compat_client, compat_headers):
    _make_llm(compat_client, compat_headers, "anchor")
    first = compat_client.get("/v2/list-retell-llms?limit=2", headers=compat_headers).json()
    lenient = compat_client.get(
        "/v2/list-retell-llms?limit=2&pagination_key=garbage", headers=compat_headers
    ).json()
    assert [i["llm_id"] for i in lenient["items"]] == [i["llm_id"] for i in first["items"]]


def test_v2_list_excludes_deleted(compat_client, compat_headers):
    keep = _make_llm(compat_client, compat_headers, "keep")
    gone = _make_llm(compat_client, compat_headers, "gone")
    assert (
        compat_client.delete(f"/delete-retell-llm/{gone}", headers=compat_headers).status_code
        == 204
    )
    body = compat_client.get("/v2/list-retell-llms", headers=compat_headers).json()
    listed = {i["llm_id"] for i in body["items"]}
    assert keep in listed
    assert gone not in listed


def test_v2_list_includes_chat_bound_llm(compat_client, compat_headers):
    """A Retell-LLM is channel-agnostic infra: a chat-agent-bound LLM must still appear."""
    llm_id = _make_llm(compat_client, compat_headers, "chat llm")
    r = compat_client.post(
        "/create-chat-agent",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm_id}},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    body = compat_client.get("/v2/list-retell-llms", headers=compat_headers).json()
    assert llm_id in {i["llm_id"] for i in body["items"]}
