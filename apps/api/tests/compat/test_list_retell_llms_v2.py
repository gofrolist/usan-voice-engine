"""Phase 7 slice 2: GET /v2/list-retell-llms — keyset cursor codec + paginated list."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

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
