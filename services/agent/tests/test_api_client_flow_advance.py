from __future__ import annotations

from typing import Any

import httpx

from usan_agent import api_client
from usan_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="wss://example.com",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="proj",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="https://api.example.com",
        JWT_SIGNING_KEY="j" * 32,
    )


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


async def test_flow_advance_returns_json_on_200(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            captured["url"] = url
            captured["json"] = json
            return _Resp(
                200, {"bound": True, "node_id": "n1", "instruction": "Hi", "is_end": False}
            )

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    out = await api_client.flow_advance(
        "11111111-1111-1111-1111-111111111111",
        _settings(),
        cursor=None,
        turns=[{"role": "user", "content": "hello"}],
    )
    assert out == {"bound": True, "node_id": "n1", "instruction": "Hi", "is_end": False}
    assert captured["url"].endswith("/v1/runtime/flow-advance")
    assert captured["json"]["turns"] == [{"role": "user", "content": "hello"}]
    assert captured["json"]["cursor"] is None
    assert "current_node_id" not in captured["json"]


async def test_flow_advance_returns_none_on_error(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, *a: Any, **k: Any) -> _Resp:
            return _Resp(500, {})

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    out = await api_client.flow_advance(
        "11111111-1111-1111-1111-111111111111",
        _settings(),
        cursor="flow-uuid:n1",
        turns=[],
    )
    assert out is None


async def test_flow_advance_sends_cursor_field(monkeypatch) -> None:
    """The opaque cursor round-trips verbatim under the 'cursor' request key."""
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            captured["json"] = json
            return _Resp(200, {"bound": True, "node_id": "n2", "cursor": "flow-uuid:n2"})

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _Client)
    out = await api_client.flow_advance(
        "11111111-1111-1111-1111-111111111111",
        _settings(),
        cursor="flow-uuid:n1",
        turns=[],
    )
    assert captured["json"]["cursor"] == "flow-uuid:n1"
    assert out is not None
    assert out["cursor"] == "flow-uuid:n2"
