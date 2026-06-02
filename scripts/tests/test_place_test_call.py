import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from place_test_call import build_request  # noqa: E402


def test_build_request_targets_calls_endpoint():
    req = build_request(
        base_url="https://api.usan.example",
        elder_id="11111111-1111-1111-1111-111111111111",
        idempotency_key="smoke-1",
        dynamic_vars={"greeting": "hi"},
    )
    assert req.full_url == "https://api.usan.example/v1/calls"
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode())
    assert body == {
        "elder_id": "11111111-1111-1111-1111-111111111111",
        "idempotency_key": "smoke-1",
        "dynamic_vars": {"greeting": "hi"},
    }


def test_build_request_strips_trailing_slash_on_base_url():
    req = build_request(
        base_url="https://api.usan.example/",
        elder_id="e",
        idempotency_key="k",
        dynamic_vars={},
    )
    assert req.full_url == "https://api.usan.example/v1/calls"
