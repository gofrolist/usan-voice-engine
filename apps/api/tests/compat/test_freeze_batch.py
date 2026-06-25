"""T047 (Task 12) — frozen contract tests for ``call_time_window`` typed echo.

Oracle shape (captured 2026-06-25):
  CallTimeWindow.windows: list[TimeWindow] (required, minItems=1)
  TimeWindow.start: number (minutes since local midnight, e.g. 540 = 09:00)
  TimeWindow.end: number (minutes since local midnight, e.g. 1020 = 17:00)
  CallTimeWindow.timezone: str | None (IANA, e.g. "America/New_York")
  CallTimeWindow.day: list[DayOfWeek] | None (full names: "Monday".."Sunday")

Native day format: 3-letter lowercase ('mon', 'tue', ..., 'sun').
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.frozen


def test_batch_call_time_window_typed_echo(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    """201 response echoes the typed CallTimeWindow including timezone."""
    body = {
        "from_number": "+15551230000",
        "tasks": [{"to_number": "+15557654321"}],
        "call_time_window": {
            "windows": [{"start": 540, "end": 1020}],
            "timezone": "America/New_York",
            "day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        },
    }
    r = compat_client.post("/create-batch-call", json=body, headers=compat_headers)
    assert r.status_code == 201, r.text
    assert r.json()["call_time_window"]["timezone"] == "America/New_York"


def test_batch_call_time_window_rejects_garbage(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    """``windows`` must be a list; a string value must yield 422."""
    body = {
        "from_number": "+15551230000",
        "tasks": [{"to_number": "+15557654321"}],
        "call_time_window": {"windows": "not-a-list"},
    }
    r = compat_client.post("/create-batch-call", json=body, headers=compat_headers)
    assert r.status_code == 422, r.text
