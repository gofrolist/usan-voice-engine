import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from usan_api.logging_config import _gcp_serialize


def _record(
    *,
    level: str = "INFO",
    message: str = "Transcript accessed",
    extra: dict | None = None,
    exception: Any = None,
) -> dict:
    return {
        "level": SimpleNamespace(name=level),
        "message": message,
        "time": datetime(2026, 6, 5, 1, 2, 3, tzinfo=UTC),
        "name": "usan_api.routers.calls",
        "function": "get_call",
        "line": 234,
        "extra": extra or {},
        "exception": exception,
    }


def test_serialize_maps_severity_and_promotes_extra():
    d = json.loads(
        _gcp_serialize(_record(level="WARNING", extra={"call_id": "abc", "segments": 9}))
    )
    assert d["severity"] == "WARNING"
    assert d["message"] == "Transcript accessed"
    assert d["call_id"] == "abc"  # bound extra promoted to top-level jsonPayload
    assert d["segments"] == 9
    assert d["logger"] == "usan_api.routers.calls:get_call:234"
    assert "timestamp" in d


def test_serialize_reserved_keys_win_over_extra():
    # A stray bound `message`/`severity` must not shadow the real ones.
    d = json.loads(_gcp_serialize(_record(extra={"message": "X", "severity": "BOGUS"})))
    assert d["message"] == "Transcript accessed"
    assert d["severity"] == "INFO"


def test_serialize_unknown_level_defaults():
    assert json.loads(_gcp_serialize(_record(level="NOTICE")))["severity"] == "DEFAULT"


def test_serialize_is_single_line_valid_json():
    line = _gcp_serialize(_record(extra={"client": "198.51.100.4"}))
    assert "\n" not in line
    json.loads(line)  # parses
