import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from usan_agent.logging_config import _gcp_serialize


def _record(
    *,
    level: str = "INFO",
    message: str = "Inbound check-in started",
    extra: dict | None = None,
    exception: Any = None,
) -> dict:
    return {
        "level": SimpleNamespace(name=level),
        "message": message,
        "time": datetime(2026, 6, 5, 1, 2, 3, tzinfo=UTC),
        "name": "usan_agent.worker",
        "function": "entrypoint",
        "line": 113,
        "extra": extra or {},
        "exception": exception,
    }


def test_serialize_maps_severity_and_promotes_extra():
    d = json.loads(_gcp_serialize(_record(level="ERROR", extra={"call_id": "abc"})))
    assert d["severity"] == "ERROR"
    assert d["message"] == "Inbound check-in started"
    assert d["call_id"] == "abc"
    assert d["logger"] == "usan_agent.worker:entrypoint:113"
    assert "timestamp" in d


def test_serialize_reserved_keys_win_over_extra():
    d = json.loads(_gcp_serialize(_record(extra={"message": "X", "severity": "BOGUS"})))
    assert d["message"] == "Inbound check-in started"
    assert d["severity"] == "INFO"


def test_serialize_unknown_level_defaults():
    assert json.loads(_gcp_serialize(_record(level="NOTICE")))["severity"] == "DEFAULT"


def test_serialize_is_single_line_valid_json():
    line = _gcp_serialize(_record(extra={"room": "usan-outbound-x"}))
    assert "\n" not in line
    json.loads(line)
